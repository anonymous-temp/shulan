"""Current release returns and renders three markers; old CRP input is inert."""
import json
import re
import pytest
from adapters.gut_barrier.frontend_template import load_static_template
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE, CLOSED_BOUNDARY_DISPLAY_TEMPLATE
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from tests.unit.gut_barrier.test_hscrp_project_display import _case


@pytest.mark.parametrize("raw,unit", [(None,"mg/L"), ("3","mg/L"), (">3","mg/L"), (">0.3","mg/dL"), ("100","mg/L")])
def test_current_callback_and_html_exclude_crp_with_unchanged_table_contract(raw,unit):
    report = build_report(_case(raw, unit=unit, abnormal=True), {}, report_id="current-preview")
    report["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    public = project_callback_report(report)
    table = next(b for s in public["sections"] for b in s["blocks"] if b["title"] == "1.2 检测项目与结果总览")
    assert set(table) == {"kind", "title", "columns", "rows"}
    assert table["columns"] == ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
    assert len(table["rows"]) == 3
    assert not re.search(r"crp|C反应蛋白|C-reactive", json.dumps(public,ensure_ascii=False), re.I)
    html = render_report_html(public, template_version=LATEST_DISPLAY_TEMPLATE)
    assert not re.search(r"crp|C反应蛋白|C-reactive", html, re.I)
    assert "三项检测结果与参考信息" in html
    assert "四项检测结果" not in html
    assert "<title>肠道屏障功能评估报告</title>" in html
    assert "<h1>肠道屏障功能评估报告</h1>" in html
    assert '@page{@bottom-center{content:"肠道屏障功能评估报告"}}' in html
    assert "肠道屏障功能与炎症状态评估报告" not in html
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html,re.S)[1]) == public
    refs = [b for s in public["sections"] for b in s["blocks"] if b["kind"] == "references"]
    assert 12 <= sum(len(b["items"]) for b in refs) <= 15


def test_legacy_v13_asset_is_not_rewritten_by_barrier_only_release():
    current = load_static_template(LATEST_DISPLAY_TEMPLATE)
    old = load_static_template(CLOSED_BOUNDARY_DISPLAY_TEMPLATE)
    assert current["templateVersion"] == "gut-barrier-static.v14"
    assert old["templateVersion"] == "gut-barrier-static.v13"
    assert "hs-CRP" in json.dumps(old,ensure_ascii=False)
    assert not re.search(r"crp|C反应蛋白|C-reactive", json.dumps(current,ensure_ascii=False),re.I)
    assert current["referencePolicy"]["pinnedPrefix"] == old["referencePolicy"]["pinnedPrefix"]
