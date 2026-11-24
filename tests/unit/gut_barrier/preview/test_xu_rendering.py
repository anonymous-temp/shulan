"""XU v3 is an explicit view, not a new classifier or callback schema."""
from copy import deepcopy
import hashlib
import html as html_module
import json
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report
from adapters.gut_barrier.latest_view import display_block, XU_ATTENTION_COLUMNS, XU_MODEL_COLUMNS
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    LEGACY_DISPLAY_TEMPLATE, PRE_XU_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE, REFINED_XU_DISPLAY_TEMPLATE,
    LATEST_DISPLAY_TEMPLATE, SOURCE_STAGE_DISPLAY_TEMPLATE, display_capabilities, resolve_display_template,
)
from modules.gut_barrier.publication import project_callback_report
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report
from tests.unit.gut_barrier.preview.support import _report
from tests.unit.gut_barrier.preview.support import figure_source, visible_source
from tests.unit.gut_barrier.preview.archive_support import historical_archive


def _block(report, title):
    return next(block for section in report['sections'] for block in section['blocks']
                if block.get('title') == title)


def _public(stage=None, score=0):
    report = project_callback_report(clinical_report(pattern='---', crp='>3.0'))
    cards = _block(report, '肠道屏障功能评分（IBFC）')['items']
    cards[0]['value'] = score
    cards.append({'label': '总体阶段', 'value': stage,
                  'detail': '本次结果见屏障与炎症分项评估。' if stage is None else None})
    # Synthetic frozen v3 fixture: historical CRP rendering is still supported.
    table = _block(report, '三项检测指标')
    table['title'] = '四项检测指标'
    table['rows'].append(['超敏C反应蛋白（hs-CRP）', '>3.0 mg/L', '＜0.5 mg/L', '超出功能医学标准范围', None])
    risks = next(section for section in report['sections'] if section['id'] == 'disease_risks')
    risks['blocks'] = [
        {'kind': 'callout', 'title': '本次关注指数', 'text': '健康关注指数，不是患病概率。', 'tone': 'default'},
        {'kind': 'table', 'title': '2.1 消化系统疾病风险评估',
         'columns': ['疾病', '风险指数', '风险等级', '机制关联'],
         'rows': [['方向甲', 0, '基础关注', '与本次资料相关。'],
                  ['方向乙', None, '专项资料不足', '缺少专项资料。']]},
        {'kind': 'list', 'title': '本章小结', 'items': ['方向甲：按已有资料核实。']},
    ]
    return report


def _embedded(source):
    return json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', source, re.S)[1])


def test_versions_have_explicit_capabilities_and_unversioned_receipts_stay_v1():
    assert LATEST_DISPLAY_TEMPLATE == "gut-barrier-static.v14"
    assert not display_capabilities(LEGACY_DISPLAY_TEMPLATE).compact_layout
    assert display_capabilities(PRE_XU_DISPLAY_TEMPLATE).compact_layout
    assert not display_capabilities(PRE_XU_DISPLAY_TEMPLATE).xu_review
    assert display_capabilities(XU_DISPLAY_TEMPLATE).xu_review
    assert not display_capabilities(XU_DISPLAY_TEMPLATE).refined_fixed_layout
    assert display_capabilities(REFINED_XU_DISPLAY_TEMPLATE).refined_fixed_layout
    report = _public()
    report['meta']['displayTemplateVersion'] = XU_DISPLAY_TEMPLATE
    assert resolve_display_template(report) == LEGACY_DISPLAY_TEMPLATE


@pytest.mark.parametrize('version,digest', [
    (LEGACY_DISPLAY_TEMPLATE, '6cbe9851b47ddff0ba117e494b815d330bac372b2abc3da07e0deb2f25176732'),
    (PRE_XU_DISPLAY_TEMPLATE, 'cf6a389bd3c35a0620bd3a5c00d91718ef21affe7775373382ccc0ad0eeb557a'),
])
def test_pre_xu_known_fixture_replays_byte_for_byte(version, digest, historical_templates):
    assert hashlib.sha256(render_report_html(_report(), template_version=version).encode()).hexdigest() == digest


@pytest.mark.parametrize('version', [LEGACY_DISPLAY_TEMPLATE, PRE_XU_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE])
def test_graph_thresholds_are_release_snapshots_not_live_scoring_globals(monkeypatch, version):
    archive = historical_archive() if version != LATEST_DISPLAY_TEMPLATE else None
    from modules.gut_barrier import rules
    if version == LATEST_DISPLAY_TEMPLATE:
        internal = clinical_report(pattern='---', crp='>3.0')
        internal['meta']['displayTemplateVersion'] = version
        report = project_callback_report(internal)
    else:
        report = _public()
    before = render_report_html(report, template_version=version, template_archive_dir=archive)
    monkeypatch.setattr(rules, 'BOUNDARIES', {'DAO': (99, 100, 200, 300)})
    monkeypatch.setattr(rules, 'LEVELS', ('CHANGED-LIVE-LABEL',))
    monkeypatch.setattr(rules, 'SCORES', {'DAO': (99,)})
    monkeypatch.setattr(rules, 'score_marker', lambda *a, **kw: pytest.fail('rendering must not score'))
    monkeypatch.setattr(rules, 'grade_score', lambda *a, **kw: pytest.fail('rendering must not grade'))
    assert render_report_html(report, template_version=version, template_archive_dir=archive) == before


@pytest.mark.parametrize('score', [0, 3.5, 15, None])
def test_unconfigured_stage_is_not_inferred_from_any_ibfc_score(score, historical_templates):
    body = visible_source(render_report_html(_public(score=score), template_version=XU_DISPLAY_TEMPLATE))
    plot = figure_source(body, 1)
    assert all(name in plot for name in ('健康人群', '亚健康人群', '疾病人群'))
    assert 'class="overall-stage selected"' not in plot
    assert '68%' not in plot and 'WHO' not in plot
    if score is not None:
        assert f'{score:g} / 15' in plot
    if score == 0:
        assert 'class="result-point" style="left:0.0000%"' in plot
        assert '>0–1<' not in plot


@pytest.mark.parametrize('stage', ['健康', '亚健康', '疾病', '健康人群', '亚健康人群', '疾病人群'])
def test_stage_selects_only_explicit_public_value(stage, historical_templates):
    body = visible_source(render_report_html(_public(stage=stage), template_version=XU_DISPLAY_TEMPLATE))
    plot = figure_source(body, 1)
    assert plot.count('class="overall-stage selected"') == 1
    assert f'class="overall-stage selected">{stage.removesuffix("人群")}人群' in plot


@pytest.mark.parametrize('stage', [False, 0, {}, [], 'unknown', '<script>alert(1)</script>'])
def test_unknown_stage_shapes_do_not_select_or_execute(stage, historical_templates):
    body = visible_source(render_report_html(_public(stage=stage), template_version=XU_DISPLAY_TEMPLATE))
    plot = figure_source(body, 1)
    assert 'class="overall-stage selected"' not in plot
    assert '<script>' not in body


@pytest.mark.parametrize('result', ['0.5 mg/L', '1.8 mg/L', '>3.0 mg/L', None])
def test_hscrp_plot_uses_only_functional_reference_not_unapproved_severity(result, historical_templates):
    report = _public()
    next(row for row in _block(report, '三项检测指标')['rows'] if 'hs-CRP' in row[0])[1] = result
    plot = figure_source(visible_source(render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)), 2)
    crp = plot.split('超敏C反应蛋白（hs-CRP）', 1)[-1]
    assert '功能医学标准值' in crp and '＜0.5 mg/L' in crp
    assert '理想' not in crp
    assert 'class="scale-segment"' not in crp
    assert '中度升高' not in crp and '轻度升高' not in crp
    if result and result.startswith('>'):
        assert 'class="range-bound"' in crp and 'class="result-point"' not in crp


@pytest.mark.parametrize('reference', ['＜0.5 mg/L', '≤0.5 mg/L', '<=0.5 mg/L', '<0.8 mg/L', '≤0.8 mg/L', None])
def test_v3_chart_keeps_frozen_published_reference_operator_and_value(reference, historical_templates):
    report = _public()
    row = next(row for row in _block(report, '三项检测指标')['rows'] if 'hs-CRP' in row[0])
    row[2] = reference
    before = deepcopy(report)
    rendered = render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)
    body = visible_source(rendered)
    plot = figure_source(body, 2).split('超敏C反应蛋白（hs-CRP）', 1)[-1]
    encoded = html_module.escape(reference, quote=True) if reference else '未提供'
    assert f'功能医学标准值：{encoded}' in plot
    if reference:
        assert f'data-label="参考区间">{encoded}</td>' in body
        assert f'功能医学标准值 {encoded}</span>' in plot
        if '0.8' in reference:
            assert '0.5' not in plot
        if '<=' in reference or '≤' in reference:
            assert '标准值 ＜' not in plot
    else:
        assert 'class="reference-zone"' not in plot and '＜0.5' not in plot
    assert report == before == _embedded(rendered)


def test_v3_restores_scores_as_attention_without_changing_callback_or_types(historical_templates):
    report = _public()
    before = deepcopy(report)
    html = render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)
    body = visible_source(html)
    assert 'data-label="健康关注指数">0</td>' in body
    assert 'data-label="健康关注指数"><span class="empty">—</span>' in body
    assert 'class="cards focus-cards"' not in body and '重点关注方向</figcaption>' not in body
    assert '方向甲：按已有资料核实。' in body
    assert _embedded(html) == report == before
    assert _block(report, '2.1 消化系统疾病风险评估')['columns'] == ['疾病', '风险指数', '风险等级', '机制关联']


def test_registered_values_have_separate_meaning_and_are_not_attention_bars(historical_templates):
    report = _public()
    table = _block(report, '2.1 消化系统疾病风险评估')
    table['rows'] = [['模型甲', 0, '模型原等级', '本项数值来自独立的已配置模型；适用范围见依据。']]
    view = display_block('disease_risks', table, report['sections'], xu=True)
    assert view['columns'] == XU_MODEL_COLUMNS
    assert view['rows'] == table['rows']
    body = visible_source(render_report_html(report, template_version=XU_DISPLAY_TEMPLATE))
    assert 'data-label="评估指数">0</td>' in body
    assert 'class="risk-fill"' not in body


@pytest.mark.parametrize('value', [0, 7.5, 80, None])
@pytest.mark.parametrize('mechanism', ['本项数值来自独立的已配置模型。', '外部报告模型，未提供量尺。'])
def test_v3_without_attention_contract_never_assumes_ten_point_model_scale(value, mechanism, historical_templates):
    report = _public()
    disease = next(section for section in report['sections'] if section['id'] == 'disease_risks')
    disease['blocks'] = [
        {'kind': 'table', 'title': '2.1 消化系统疾病风险评估',
         'columns': ['疾病', '风险指数', '风险等级', '机制关联'],
         'rows': [['独立方向', value, '模型原等级', mechanism]]}]
    rendered = render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)
    body = visible_source(rendered)
    assert 'class="risk-track"' not in body and 'class="risk-number"' not in body
    assert 'class="risk-fill"' not in body
    assert 'data-label="评估指数"' in body and '独立方向' in body
    if value is not None:
        assert f'data-label="评估指数">{value}</td>' in body
    assert _embedded(rendered) == report


def test_v3_figures_are_contiguous_even_when_optional_disease_chart_is_absent(historical_templates):
    report = _public()
    for remove_chart in (False, True):
        if remove_chart:
            _block(report, '2.1 消化系统疾病风险评估')['rows'] = []
        body = visible_source(render_report_html(report, template_version=XU_DISPLAY_TEMPLATE))
        figures = re.findall(r'<figure class="report-figure" id="figure-(\d+)"', body)
        assert len(figures) == (7 if remove_chart else 8)
        assert figures == [str(i) for i in range(1, len(figures) + 1)]
        repair = next(fragment for fragment in re.findall(r'<figure.*?</figure>', body, re.S)
                      if '肠道修复五阶段原则' in fragment)
        assert 'FMT' in repair and 'PFMT' not in repair
        for text in ('去除诱因，阻止持续性损伤', '补充营养，改善肠上皮细胞能量代谢与黏膜微循环',
                     '重建肠道生态，黏液层快速修复与再生', '靶向调节，巩固定植，修复紧密连接与免疫屏障',
                     '维持肠道稳态，实现机体整体健康'):
            assert text in repair


def test_v3_expansion_is_idempotent_and_retains_unknown_safety_blocks(historical_templates):
    report = _public()
    report['sections'][0]['blocks'].append({'kind': 'callout', 'title': '个体限制', 'text': 'SAFETY-PRESERVED', 'tone': 'caution'})
    before = deepcopy(report)
    expanded = expand_dynamic_report(report, template_version=XU_DISPLAY_TEMPLATE)
    assert expand_dynamic_report(expanded, template_version=XU_DISPLAY_TEMPLATE) == expanded
    assert report == before
    html = render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)
    assert 'SAFETY-PRESERVED' in visible_source(html)
    assert _embedded(html) == before


def test_partial_unknown_layout_does_not_hide_unrendered_stage_or_safety_information(historical_templates):
    report = _public()
    report['sections'].append({'id': 'future', 'title': '附加资料', 'blocks': [], 'visible': True})
    html = render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)
    assert '总体阶段' in visible_source(html)
    assert _embedded(html) == report
