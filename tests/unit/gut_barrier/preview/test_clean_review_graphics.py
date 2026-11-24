"""v5 source-only hs-CRP axes and concise chart-reading captions."""
from copy import deepcopy
from hashlib import sha256
import html
import re

import pytest

from adapters.gut_barrier.graphics import _indicator_plot, after_block, before_section
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import CLEAN_REVIEW_DISPLAY_TEMPLATE, REFINED_XU_DISPLAY_TEMPLATE
from tests.unit.gut_barrier.preview.support import _report
from tests.unit.gut_barrier.preview.test_xu_rendering import _public, _block


def _marker_block(result='>3.0 mg/L', reference='＜0.5 mg/L'):
    return {
        'kind': 'table', 'title': '三项检测指标',
        'columns': ['检测指标', '检测结果', '参考范围', '结果评价', '单项评分'],
        'rows': [['超敏C反应蛋白（hs-CRP）', result, reference, '现有结果评价', None]],
    }


def _plot(block, clean=True):
    return _indicator_plot(block, template_version=REFINED_XU_DISPLAY_TEMPLATE,
                           clean_review_layout=clean)


def test_old_range_axis_problem_reproduces_but_clean_view_has_no_invented_numeric_limit():
    block = _marker_block()
    before = deepcopy(block)
    assert '3.6 mg/L' in _plot(block, clean=False)
    result = _plot(block)
    assert '3.6 mg/L' not in result
    assert '界值 3.0 mg/L（不含界值）' in result
    assert 'data-result-direction="greater"' in result
    assert 'data-boundary-kind="open"' in result
    assert 'fill="#ffffff"' in result
    assert '&gt;3.0 mg/L' in result
    assert before == block


@pytest.mark.parametrize('result,direction,kind,boundary', [
    ('>3.0 mg/L', 'greater', 'open', '3.0'),
    ('>=3.0 mg/L', 'greater', 'closed', '3.0'),
    ('≥3.0 mg/L', 'greater', 'closed', '3.0'),
    ('＞3.0 mg/L', 'greater', 'open', '3.0'),
    ('<1.0 mg/L', 'less', 'open', '1.0'),
    ('<=0.5 mg/L', 'less', 'closed', '0.5'),
    ('≤0.5 mg/L', 'less', 'closed', '0.5'),
    ('＜0.5 mg/L', 'less', 'open', '0.5'),
    ('0.0 mg/L', 'exact', 'exact', '0.0'),
    ('1.8 mg/L', 'exact', 'exact', '1.8'),
    ('=3.0 mg/L', 'exact', 'exact', '3.0'),
])
def test_source_axis_preserves_comparison_boundary_inclusivity_and_exact_results(result, direction, kind, boundary):
    output = _plot(_marker_block(result))
    assert f'data-result-direction="{direction}"' in output
    assert f'data-boundary-kind="{kind}"' in output
    assert html.escape(result, quote=True) in output
    axis = re.search(r'<svg\b.*?</svg>', output, re.S)[0]
    labels = [html.unescape(value) for value in re.findall(r'<text\b[^>]*>(.*?)</text>', axis)]
    assert labels[0] == '功能医学标准值 ＜0.5 mg/L'
    assert labels[1].startswith(('结果 ' if direction == 'exact' else '界值 ') + boundary + ' mg/L')
    # Only source numbers occur in visible SVG labels; dimensions are geometry.
    assert set(re.findall(r'\d+(?:\.\d+)?', ' '.join(labels))) == {'0.5', boundary}
    if kind == 'open':
        assert '（不含界值）' in labels[1] and 'fill="#ffffff"' in axis
    elif kind == 'closed':
        assert '（含界值）' in labels[1] and 'fill="#1b5d4c"' in axis
    else:
        assert '界值' not in labels[1]
        assert 'stroke="#915828"' not in axis


@pytest.mark.parametrize('result,reference', [
    (None, '＜0.5 mg/L'), ('', '＜0.5 mg/L'),
    ('>3.0 U/L', '＜0.5 mg/L'), ('不详', '＜0.5 mg/L'),
    ('>3.0 mg/L', None), ('>3.0 mg/L', '参考范围未知'),
])
def test_unavailable_or_incompatible_measurement_is_not_given_a_synthetic_axis(result, reference):
    output = _plot(_marker_block(result, reference))
    assert 'hscrp-source-axis' not in output
    assert '未提供可绘制的定量区间' in output


def test_three_injury_marker_graphs_are_unchanged_in_clean_mode():
    report = _public()
    block = deepcopy(_block(report, '四项检测指标'))
    block['rows'] = [row for row in block['rows'] if 'hs-CRP' not in row[0]]
    old, clean = _plot(block, clean=False), _plot(block)
    # Figure caption may be concise; the plotted data and scales are identical.
    assert old.split('<p class="figure-note">')[0] == clean.split('<p class="figure-note">')[0]


def test_entrypoints_forward_clean_mode_and_attention_caption_only_explains_graph_reading():
    report = _public()
    rows = _block(report, '2.1 消化系统疾病风险评估')['rows']
    rows.append(['胃炎随访', None, '随访关注', '既往检查依据。'])
    before = deepcopy(report)
    plot = after_block('overview', _block(report, '四项检测指标'), report['sections'],
                       template_version=REFINED_XU_DISPLAY_TEMPLATE, clean_review_layout=True)
    assert 'hscrp-source-axis' in plot and '3.6 mg/L' not in plot
    chart = before_section('disease_risks', report['sections'],
                           template_version=REFINED_XU_DISPLAY_TEMPLATE, clean_review_layout=True)
    note = re.search(r'<p class="figure-note">(.*?)</p>', chart)[1]
    assert note == '横条与数字表示本次健康关注指数；“定性随访”项目不绘制分值横条。'
    assert all(word not in note for word in ('自定义', '概率', '临床验证', '治疗决策', 'IBFC'))
    old_chart = before_section('disease_risks', report['sections'], template_version=REFINED_XU_DISPLAY_TEMPLATE)
    assert chart.split('<p class="figure-note">')[0] == old_chart.split('<p class="figure-note">')[0]
    assert '定性随访' in chart and '0 / 10' in chart
    assert report == before


def test_v5_age_figure_uses_reading_guidance_without_review_workflow_wording():
    block = {'kind': 'paragraph', 'title': '4.6 炎症性衰老：当慢火遇上时间'}
    sections = _public()['sections']
    current = after_block(
        'education', block, sections, latest=True,
        template_version=CLEAN_REVIEW_DISPLAY_TEMPLATE, clean_review_layout=True,
    )
    note = re.search(r'<p class="figure-note">(.*?)</p>', current, re.S)[1]
    assert note == '研究背景示意：双向箭头表示相关因素可能相互影响，不表示确定的因果方向。'
    assert all(phrase not in note for phrase in (
        '图中不设年龄倍数', '未经核实的数值曲线', '个人炎症水平', '受检者年龄趋势图'))

    historical = after_block(
        'education', block, sections, latest=True,
        template_version=REFINED_XU_DISPLAY_TEMPLATE, clean_review_layout=False,
    )
    assert '图中不设年龄倍数或未经核实的数值曲线' in historical


@pytest.mark.parametrize('version,digest', [
    ('gut-barrier-static.v1', '6cbe9851b47ddff0ba117e494b815d330bac372b2abc3da07e0deb2f25176732'),
    ('gut-barrier-static.v2', 'cf6a389bd3c35a0620bd3a5c00d91718ef21affe7775373382ccc0ad0eeb557a'),
    ('gut-barrier-static.v3', '447c6fe7e74269e4c30a5ed25c16628ffff43bb1d5db5eb2953cc65126261ac0'),
    ('gut-barrier-static.v4', 'd6eb413e68e77993158f146a76a9e106357cdfa5d4035f55a71a808db9f662e2'),
])
def test_all_four_frozen_versions_keep_identical_html_bytes(version, digest, historical_templates):
    assert sha256(render_report_html(_report(), template_version=version).encode()).hexdigest() == digest
