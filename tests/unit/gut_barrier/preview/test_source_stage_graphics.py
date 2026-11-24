"""v6 reproduces the source stage-band layout without manufacturing a stage."""
from copy import deepcopy
from hashlib import sha256
import re

import pytest

pytestmark = pytest.mark.usefixtures("historical_templates")

from adapters.gut_barrier.graphics import _ibfc_plot
from adapters.gut_barrier.latest_view import display_block
from adapters.gut_barrier.rendering import render_report_html, _SOURCE_STAGE_CSS
from tests.unit.gut_barrier.preview.support import _report
from tests.unit.gut_barrier.preview.support import visible_source
from tests.unit.gut_barrier.preview.test_xu_rendering import _public, _block, _embedded


VERSION = 'gut-barrier-static.v6'


def _html(report):
    return render_report_html(report, template_version=VERSION)


def _ibfc_figure(source):
    return next(value for value in re.findall(r'<figure\b.*?</figure>', visible_source(source), re.S)
                if 'source-stage-ibfc' in value)


@pytest.mark.parametrize('score,display', [(0, '0'), (1, '1'), (3.5, '3.5'), (15, '15'), (None, '未获得评分')])
def test_source_graph_displays_only_actual_score_without_denominator_or_inferred_stage(score, display):
    report = _public(score=score)
    before = deepcopy(report)
    source = _html(report)
    graph = _ibfc_figure(source)
    assert f'<strong>{display}</strong>' in graph
    assert '肠道屏障损伤评估指数（IBFC）综合评估' in graph
    assert graph.count('class="source-stage-label"') == 3
    assert all(f'>{name}</span>' in graph for name in ('健康人群', '亚健康人群', '疾病人群'))
    assert 'class="source-stage-label selected"' not in graph
    assert all(word not in graph for word in ('/ 15', '0–15', '满分', '健康阶段概念', 'result-point', '68%', 'WHO', 'interval-labels'))
    assert all(word not in graph for word in ('未配置', '阈值', '不代表', '尚未'))
    assert 'style="left:' not in graph and 'scale-tick' not in graph
    if score is None:
        assert '<strong>0</strong>' not in graph
    assert report == before == _embedded(source)


@pytest.mark.parametrize('stage,label', [
    ('健康', '健康人群'), ('健康人群', '健康人群'), ('healthy', '健康人群'),
    ('亚健康', '亚健康人群'), ('亚健康人群', '亚健康人群'), ('subhealth', '亚健康人群'),
    ('疾病', '疾病人群'), ('疾病人群', '疾病人群'), ('disease', '疾病人群'),
])
def test_only_an_explicit_valid_stage_highlights_a_label_not_a_numeric_position(stage, label):
    graph = _ibfc_figure(_html(_public(stage=stage, score=0)))
    assert graph.count('class="source-stage-label selected"') == 1
    assert f'class="source-stage-label selected" aria-current="true">{label}</span>' in graph
    assert 'result-point' not in graph and 'left:' not in graph and '%' not in graph


@pytest.mark.parametrize('stage', [False, 0, {}, [], 'unknown', '<script>alert(1)</script>'])
def test_invalid_or_untrusted_stage_does_not_select_a_label_or_execute_markup(stage):
    graph = _ibfc_figure(_html(_public(stage=stage)))
    assert 'class="source-stage-label selected"' not in graph
    assert '<script>' not in graph and 'alert(1)' not in graph


@pytest.mark.parametrize('score', [False, True, '0', {}, [], -1, 16, '<img src=x onerror=alert(1)>'])
def test_invalid_score_is_not_coerced_to_zero_or_rendered_as_markup(score):
    report = _public(score=score)
    graph = _ibfc_figure(_html(report))
    assert '<strong>未获得评分</strong>' in graph
    assert '<strong>0</strong>' not in graph
    assert '<img' not in graph


def test_duplicate_numeric_card_is_consumed_but_nonstandard_details_and_extra_cards_survive():
    report = _public(score=0)
    block = _block(report, '肠道屏障功能评分（IBFC）')
    next(item for item in block['items'] if item['label'] == 'IBFC评分')['detail'] = '补充信息：建议结合既往检测趋势。'
    block['items'].append({'label': '额外检查结果', 'value': False, 'detail': '已明确阴性'})
    before = deepcopy(report)
    output = display_block('overview', block, report['sections'], xu=True, source_stage_layout=True)
    assert output is not None
    assert all(item['label'] != 'IBFC评分' for item in output['items'])
    assert any(item['label'] == '额外检查结果' and item['value'] is False for item in output['items'])
    source = _html(report)
    body = visible_source(source)
    assert body.count('补充信息：建议结合既往检测趋势。') == 1
    assert '<div class="fact-label">IBFC评分</div>' not in body
    assert '额外检查结果' in body and '>False</div>' in body
    assert report == before == _embedded(source)


@pytest.mark.parametrize('detail', ['满分15分', '满分 15 分。', None])
def test_only_standard_maximum_caption_is_omitted(detail):
    report = _public(score=0)
    block = _block(report, '肠道屏障功能评分（IBFC）')
    next(item for item in block['items'] if item['label'] == 'IBFC评分')['detail'] = detail
    body = visible_source(_html(report))
    assert '满分' not in _ibfc_figure(body)
    assert '<div class="fact-label">IBFC评分</div>' not in body
    assert _embedded(_html(report)) == report


def test_unknown_detail_is_escaped_not_deleted_when_numeric_card_is_hidden():
    report = _public(score=0)
    block = _block(report, '肠道屏障功能评分（IBFC）')
    next(item for item in block['items'] if item['label'] == 'IBFC评分')['detail'] = '<script>clinical-note</script>'
    graph = _ibfc_figure(_html(report))
    assert '&lt;script&gt;clinical-note&lt;/script&gt;' in graph
    assert '<script>' not in graph


def test_ambiguous_or_future_score_cards_are_not_partially_dropped():
    report = _public()
    block = _block(report, '肠道屏障功能评分（IBFC）')
    block['items'].append({'label': 'IBFC评分', 'value': 7, 'detail': '另一项资料'})
    assert display_block('overview', block, report['sections'], xu=True, source_stage_layout=True) == block
    assert _ibfc_plot(block, source_stage_layout=True) == ''


def test_source_style_css_is_bound_to_new_release_with_mobile_and_print_layout():
    source = _html(_public())
    assert _SOURCE_STAGE_CSS in source
    assert 'linear-gradient(90deg,#7da53d' in _SOURCE_STAGE_CSS
    assert '@media(max-width:540px)' in _SOURCE_STAGE_CSS
    assert '@media print' in _SOURCE_STAGE_CSS
    assert '@media print{.folio{break-after:avoid;page-break-after:avoid}' in _SOURCE_STAGE_CSS
    assert 'print-color-adjust:exact' in _SOURCE_STAGE_CSS


@pytest.mark.parametrize('version,digest', [
    ('v1', '6cbe9851b47ddff0ba117e494b815d330bac372b2abc3da07e0deb2f25176732'),
    ('v2', 'cf6a389bd3c35a0620bd3a5c00d91718ef21affe7775373382ccc0ad0eeb557a'),
    ('v3', '447c6fe7e74269e4c30a5ed25c16628ffff43bb1d5db5eb2953cc65126261ac0'),
    ('v4', 'd6eb413e68e77993158f146a76a9e106357cdfa5d4035f55a71a808db9f662e2'),
    ('v5', 'ede6191e64e0375533027003cda7fedc39e9805410c18ee2fb3ed33605cde51a'),
])
def test_v1_through_v5_rendered_html_bytes_remain_unchanged(version, digest, historical_templates):
    source = render_report_html(_report(), template_version='gut-barrier-static.' + version)
    assert sha256(source.encode()).hexdigest() == digest


def test_source_stage_css_is_not_injected_into_archived_v5(historical_templates):
    old = render_report_html(_public(), template_version='gut-barrier-static.v5')
    assert _SOURCE_STAGE_CSS not in old
