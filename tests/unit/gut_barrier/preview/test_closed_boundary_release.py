"""The current release uses confirmed intervals and preserves callback data."""
from copy import deepcopy

import pytest

from adapters.gut_barrier.frontend_template import load_static_template
from adapters.gut_barrier.graphics import _indicator_plot
from modules.gut_barrier.display_versions import (
    CLOSED_BOUNDARY_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE,
    display_capabilities,
)
from modules.gut_barrier.reference_pins import pinned_references


def _frozen_marker_block():
    return {
        "kind": "table", "title": "三项检测指标",
        "columns": ["检测指标", "检测结果", "参考范围", "结果评价", "单项评分"],
        "rows": [
            ["D-乳酸", "30 mg/L", "＜14 mg/L", "原冻结评价", None],
            ["DAO", "20 U/L", "＜8.5 U/L", "原冻结评价", None],
            ["LPS", "35 U/L", "＜18 U/L", "原冻结评价", None],
        ],
    }


def test_v13_uses_closed_intervals_ibfc_explanation_and_fixed_reference_policy():
    assert LATEST_DISPLAY_TEMPLATE != CLOSED_BOUNDARY_DISPLAY_TEMPLATE
    assert display_capabilities(LATEST_DISPLAY_TEMPLATE).barrier_only_layout
    capabilities = display_capabilities(CLOSED_BOUNDARY_DISPLAY_TEMPLATE)
    assert capabilities.closed_boundary_layout
    assert capabilities.template_risk_layout
    assert capabilities.pinned_reference_layout
    asset = load_static_template(CLOSED_BOUNDARY_DISPLAY_TEMPLATE)
    assert asset["templateVersion"] == CLOSED_BOUNDARY_DISPLAY_TEMPLATE
    policy = asset["referencePolicy"]
    assert policy["mode"] == "pinned_prefix_selected_display"
    assert policy["targetRange"] == [12, 15]
    assert policy["allowOmittedFixedCitations"] is True
    assert policy["pinnedPrefix"] == pinned_references()
    assert len(policy["pinnedPrefix"]) == 8
    assert policy["pinnedPrefix"][0]["doi"] == "10.13381/j.cnki.cjm.2009.01.018"
    assert asset["sourceVersions"]["templateVersion"] == "gut-barrier-closed-boundary-review-20261008-v1"
    explanation = next(block for block in asset["appendixBlocks"] if block.get("title") == "风险评估结果说明")
    assert "屏障三项按同一领域取最大贡献" not in explanation["text"]
    assert "单项积分相加得到IBFC" in explanation["text"]
    assert "屏障贡献按IBFC损伤等级系数与病种屏障权重计算" in explanation["text"]


@pytest.mark.parametrize("spans", [
    ("＜14", "≥14且＜17", "≥17且＜22", "≥22且＜30", "≥30"),
    ("＜8.5", "≥8.5且＜11", "≥11且＜16", "≥16且＜20", "≥20"),
    ("＜18", "≥18且＜22", "≥22且＜28", "≥28且＜35", "≥35"),
])
def test_new_marker_graph_labels_include_every_confirmed_lower_boundary(spans):
    block = _frozen_marker_block()
    before = deepcopy(block)
    plot = _indicator_plot(block, template_version=CLOSED_BOUNDARY_DISPLAY_TEMPLATE)
    assert all(f"<small>{span}</small>" in plot for span in spans)
    assert all(f"<small>＞{point}</small>" not in plot for point in (30, 20, 35))
    assert plot.count('class="result-point"') == 3
    assert block == before
    # The renderer consumes a callback's evaluation and never reclassifies it.
    assert plot.count("原冻结评价") == 6

