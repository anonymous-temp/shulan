"""A public bibliography is selected; complete claim sources remain private."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.reference_order import (
    PRIMARY_TITLE, REFERENCE_FIELDS, bind_ordered_static_copy, load_reference_asset,
    publish_ordered_references,
)
from tests.unit.gut_barrier.test_ordered_references import fixture, ref, references, section
from tests.unit.gut_barrier.test_product_display_feedback import _source


def _budget_fixture(count=24):
    items = [ref(f"Scoped source {index}", f"https://example.test/{index}")
             for index in range(1, count + 1)]
    rows = [[f"Disease {index}", 1 if index <= count - 3 else 7 + (index % 3) / 10,
             "低" if index <= count - 3 else "高", f"Mechanism {index}.[{index}]"]
            for index in range(1, count + 1)]
    report = {"schemaVersion": "gut-barrier-public.v2", "meta": {"title": "test", "reportId": "budget"},
              "sections": [section("disease_risks", [{"kind": "table", "title": "Disease mechanisms",
                            "columns": ["疾病", "风险指数", "风险等级", "机制关联"], "rows": rows}]),
                           section("appendix", [{"kind": "references", "title": PRIMARY_TITLE,
                                                  "items": items}])]}
    asset = {"sectionOrder": ["disease_risks", "appendix"], "fixedSections": [],
             "overviewInsertions": [], "appendixBlocks": []}
    return report, deepcopy(report), asset


def test_selected_budget_keeps_all_mechanisms_and_complete_original_source_mapping():
    report, source, asset = _budget_fixture()
    original_source, original_asset = deepcopy(source), deepcopy(asset)
    audit = {"existing": "retained"}
    inventory = publish_ordered_references(report, source, asset, reference_limit=15, reference_audit=audit)
    main = references(report, PRIMARY_TITLE)
    assert len(main) == 15
    rows = report["sections"][0]["blocks"][0]["rows"]
    assert len(rows) == 24
    assert [re.sub(r"\[.*?\]", "", row[3]) for row in rows] == [f"Mechanism {i}." for i in range(1, 25)]
    assert len(inventory) == 48  # Dynamic and original-source catalogs both retained.
    ledger = audit["referencePublication"]
    assert ledger["inventory"] == inventory and ledger["originalCitedCount"] == 24
    assert ledger["displayedCount"] == 15 and ledger["targetRange"] == [12, 15]
    assert len(ledger["claims"]) == 24 and all(claim["sourceRoots"] for claim in ledger["claims"])
    omitted = [item for item in inventory if item["selection"] == "budget_omitted"]
    assert len(omitted) == 18 and all(item["omissionReason"] == "selected_display_budget" for item in omitted)
    numbers = [int(number) for row in rows for number in re.findall(r"\[(\d+)\]", row[3])]
    assert list(dict.fromkeys(numbers)) == list(range(1, 16))
    assert all(set(item) == set(REFERENCE_FIELDS) for item in main)
    assert source == original_source and asset == original_asset and audit["existing"] == "retained"


def test_budget_prioritizes_highest_three_current_risk_mechanisms_without_changing_scores():
    report, source, asset = _budget_fixture()
    publish_ordered_references(report, source, asset, reference_limit=3)
    assert {item["citation"] for item in references(report, PRIMARY_TITLE)} == {
        "Scoped source 22", "Scoped source 23", "Scoped source 24"}
    rows = report["sections"][0]["blocks"][0]["rows"]
    assert [row[:3] for row in rows] == [row[:3] for row in source["sections"][0]["blocks"][0]["rows"]]


def test_exact_reviewed_supplied_source_is_preferred_after_current_top_risks():
    from modules.gut_barrier.reference_priority import preferred_mechanism
    report, source, asset = _budget_fixture()
    reviewed = preferred_mechanism("ibs")["sources"][0]
    references(report, PRIMARY_TITLE)[0] = {key: reviewed.get(key) for key in REFERENCE_FIELDS}
    publish_ordered_references(report, source, asset, reference_limit=4)
    assert reviewed["doi"] in {item["doi"] for item in references(report, PRIMARY_TITLE)}


def test_true_sources_below_target_are_never_padded_with_uncited_publications():
    report, source, asset = fixture()
    audit = {}
    publish_ordered_references(report, source, asset, reference_limit=15, reference_audit=audit)
    assert len(references(report, PRIMARY_TITLE)) == 3
    assert audit["referencePublication"]["displayedCount"] == 3
    assert all(item["selection"] != "budget_omitted" for item in audit["referencePublication"]["inventory"])
    assert all("Uncited" not in item["citation"] for item in references(report, PRIMARY_TITLE))


def test_fixed_required_sources_over_limit_fail_before_mutating_report():
    report, source, asset = fixture()
    original = deepcopy(report)
    with pytest.raises(ValueError, match="fixed body references exceed"):
        publish_ordered_references(report, source, asset, reference_limit=1)
    assert report == original


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, "15"])
def test_invalid_budget_is_not_silently_accepted(limit):
    report, source, asset = fixture()
    with pytest.raises(ValueError, match="positive integer"):
        publish_ordered_references(report, source, asset, reference_limit=limit)


def test_latest_projection_selects_fifteen_and_keeps_static_binding_and_input_pure():
    source = _source()
    source["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    original = deepcopy(source)
    audit = {}
    report = project_callback_report(source, reference_audit=audit)
    assert len(references(report, PRIMARY_TITLE)) == 15
    assert source == original
    assert len(audit["referencePublication"]["claims"]) > 15
    assert any(item["selection"] == "budget_omitted" for item in audit["referencePublication"]["inventory"])
    bound = bind_ordered_static_copy(load_reference_asset(LATEST_DISPLAY_TEMPLATE), report,
                                    allow_omitted_fixed=True)
    assert references({"sections": [section("appendix", bound["appendixBlocks"])]}, PRIMARY_TITLE) == references(report, PRIMARY_TITLE)
    public = json.dumps(report, ensure_ascii=False)
    assert not any(key in public for key in ("referencePublication", "inventory", "sourceRoot", "omissionReason"))
    frozen_audit = {}
    assert project_callback_report(report, reference_audit=frozen_audit) == report
    assert frozen_audit == {}  # No re-selection or rewriting of a frozen receipt.


@pytest.mark.asyncio
async def test_service_persists_complete_reference_audit_only_in_private_job_audit():
    from tests.unit.gut_barrier.test_service import environment, request
    report = _source()
    report["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    pipeline = SimpleNamespace(version="budget-test", generate=AsyncMock(return_value=(report, {"partial": False})))
    env = environment(pipeline=pipeline)
    job = await env.service.submit(request())
    assert await env.service.drain_generation() == 1
    saved = env.collection.documents[job["_id"]]
    ledger = saved["audit"]["referencePublication"]
    assert ledger["displayedCount"] == 15 and ledger["originalCitedCount"] > 15
    assert any(item["selection"] == "budget_omitted" for item in ledger["inventory"])
    assert any(not claim["selectedBodyNumbers"] for claim in ledger["claims"])
    assert len(references(saved["body"]["report"], PRIMARY_TITLE)) == 15
    assert "referencePublication" not in json.dumps(saved["body"], ensure_ascii=False)
    assert saved["audit"]["reportDocument"] == report
