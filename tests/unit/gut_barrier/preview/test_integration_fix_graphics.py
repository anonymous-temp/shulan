"""Callback-supplied chapter two chart and summary retain their display order."""
from copy import deepcopy
import json
import re

from adapters.gut_barrier.graphics import (
    ATTENTION_OVERVIEW_COLUMNS, ATTENTION_OVERVIEW_TITLE, RISK_COLUMNS,
    after_block, before_section,
)
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import CANONICAL_DISPLAY_TEMPLATE, WORD_REVIEW_DISPLAY_TEMPLATE
from tests.unit.gut_barrier.preview.support import visible_source


def chapter_blocks():
    return [
        {"kind": "paragraph", "title": None, "text": "本次资料导语。"},
        {"kind": "callout", "title": "本次关注指数", "text": "本次关注说明。", "tone": "info"},
        {"kind": "table", "title": ATTENTION_OVERVIEW_TITLE,
         "columns": list(ATTENTION_OVERVIEW_COLUMNS), "rows": [
             ["消化系统", "方向甲", 0, "基础关注"],
             {"cells": ["皮肤系统", "方向乙", None, "随访关注"], "current": False},
             ["消化系统", "方向丙", 7, "优先评估"],
         ]},
        {"kind": "table", "title": "2.1 消化系统疾病风险评估", "columns": list(RISK_COLUMNS),
         "rows": [["方向甲", 0, "基础关注", "本次依据甲"],
                  ["方向丙", 7, "优先评估", "本次依据丙"],
                  ["独立模型", 9, "低风险", "本项数值来自独立的已配置模型，不与本章自定义关注指数比较。"]]},
        {"kind": "table", "title": "2.2 皮肤系统疾病风险评估", "columns": list(RISK_COLUMNS),
         "rows": [["方向乙", None, "随访关注", "本次依据乙"]]},
        {"kind": "paragraph", "title": "2.3 本章小结", "text": "本次结论正文，结合现有资料继续随访。"},
        {"kind": "list", "title": "2.3 本章小结", "items": ["消化系统关注方向：方向甲、方向丙。", "皮肤系统关注方向：方向乙。"]},
    ]


def sections(blocks):
    return [{"id": "disease_risks", "title": "第二章", "blocks": blocks, "visible": True}]


def test_current_plot_uses_published_overview_at_its_block_position_and_preserves_zero_null():
    blocks = chapter_blocks()
    # An already-published overview owns its values; the renderer must not
    # silently rebuild it from a separate detailed table while replaying.
    blocks[3]["rows"][0][1] = 4
    source = sections(blocks)
    original = deepcopy(source)
    assert before_section("disease_risks", source, template_version=CANONICAL_DISPLAY_TEMPLATE) == ""
    chart = after_block("disease_risks", blocks[2], source, template_version=CANONICAL_DISPLAY_TEMPLATE)
    assert chart.count('class="risk-fill"') == 2
    assert 'aria-label="方向甲：健康关注指数 0 / 10"' in chart
    assert 'aria-label="方向甲：健康关注指数 4 / 10"' not in chart
    assert 'aria-label="方向丙：健康关注指数 7 / 10"' in chart
    assert 'class="risk-qualitative">定性随访' in chart
    assert chart.count('<h4>消化系统</h4>') == 1
    assert "独立模型" not in chart
    assert source == original


def test_explicit_empty_overview_does_not_fall_back_to_unrelated_scores():
    blocks = chapter_blocks()
    blocks[2]["rows"] = []
    source = sections(blocks)
    assert before_section("disease_risks", source, template_version=CANONICAL_DISPLAY_TEMPLATE) == ""
    assert after_block("disease_risks", blocks[2], source, template_version=CANONICAL_DISPLAY_TEMPLATE) == ""


def test_current_plot_without_overview_and_historical_plot_keep_disease_table_fallback():
    blocks = chapter_blocks()
    source = sections(blocks)
    historical = before_section("disease_risks", source, template_version=WORD_REVIEW_DISPLAY_TEMPLATE)
    assert 'aria-label="方向丙：健康关注指数 7 / 10"' in historical
    assert after_block("disease_risks", blocks[2], source, template_version=WORD_REVIEW_DISPLAY_TEMPLATE) == ""
    blocks.pop(2)
    current = before_section("disease_risks", source, template_version=CANONICAL_DISPLAY_TEMPLATE)
    assert 'aria-label="方向甲：健康关注指数 0 / 10"' in current
    assert "独立模型" not in current


def test_preview_places_introduction_then_chart_before_subsection_and_renders_one_summary_heading(historical_templates):
    blocks = chapter_blocks()
    report = {"schemaVersion": "gut-barrier-public.v2", "meta": {"title": "合成报告"}, "sections": [
        {"id": "overview", "title": "第一章 检测综述", "visible": True, "blocks": []},
        *sections(blocks),
        {"id": "appendix", "title": "第四章 附录", "visible": True, "blocks": []},
    ]}
    original = deepcopy(report)
    rendered = render_report_html(report, template_version=CANONICAL_DISPLAY_TEMPLATE)
    body = visible_source(rendered).split('<section class="report-section" id="disease_risks">', 1)[1].split('</section>', 1)[0]
    assert body.index("本次资料导语。") < body.index("本次关注说明。") < body.index('class="risk-grid"')
    assert body.index('class="risk-grid"') < body.index("2.1 消化系统疾病风险评估")
    assert 'data-label="健康关注指数"' not in body
    assert body.count("2.3 本章小结") == 1
    assert body.index("2.3 本章小结") < body.index("本次结论正文") < body.index("消化系统关注方向")
    assert "独立模型" in body  # Its original detailed row remains visible.
    embedded = json.loads(re.search(r'id="report-data">(.*?)</script>', rendered, re.S)[1])
    assert embedded == original == report
