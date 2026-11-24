from tests.unit.gut_barrier.preview.archive_support import historical_asset
"""v4 presentation refinements never rewrite a frozen receipt or old release."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.usefixtures("historical_templates")

from adapters.gut_barrier.frontend_template import expand_dynamic_report, load_static_template
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    LATEST_DISPLAY_TEMPLATE, REFINED_XU_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE, SOURCE_STAGE_DISPLAY_TEMPLATE,
    display_capabilities, resolve_display_template,
)
from tests.unit.gut_barrier.preview.support import _report
from tests.unit.gut_barrier.preview.support import visible_source
from tests.unit.gut_barrier.preview.test_xu_rendering import _public, _block, _embedded


HISTORICAL = {
    'v1': ('62b4a6ebc0b9f0c7c8e4b09e05bfeaebbb876bd3e20431bcf9b41e18983ce9a0',
           '6cbe9851b47ddff0ba117e494b815d330bac372b2abc3da07e0deb2f25176732'),
    'v2': ('5162b8268c709db333b3a76e4e52daf75ef6e0db01db203102f14243e056036c',
           'cf6a389bd3c35a0620bd3a5c00d91718ef21affe7775373382ccc0ad0eeb557a'),
    'v3': ('e8cfb540c5398b1b26c1b089c27660b2b2674d39c08dfa897ead4f4002bb705c',
           '447c6fe7e74269e4c30a5ed25c16628ffff43bb1d5db5eb2953cc65126261ac0'),
}


def _html(report=None):
    return render_report_html(report if report is not None else _public(), template_version=REFINED_XU_DISPLAY_TEMPLATE)


def _figure(body, title):
    return next(match for match in re.findall(r'<figure\b.*?</figure>', body, re.S) if title in match)


@pytest.mark.parametrize('version', HISTORICAL)
def test_historical_assets_and_html_remain_byte_identical(version):
    asset_hash, html_hash = HISTORICAL[version]
    assert sha256(historical_asset(version).read_bytes()).hexdigest() == asset_hash
    assert sha256(render_report_html(_report(), template_version=f'gut-barrier-static.{version}').encode()).hexdigest() == html_hash


def test_only_explicit_new_release_uses_refined_capability():
    assert LATEST_DISPLAY_TEMPLATE == 'gut-barrier-static.v13'
    assert display_capabilities(REFINED_XU_DISPLAY_TEMPLATE).refined_fixed_layout
    assert not display_capabilities(XU_DISPLAY_TEMPLATE).refined_fixed_layout
    assert resolve_display_template(_public()) == 'gut-barrier-static.v1'
    current, previous = load_static_template(REFINED_XU_DISPLAY_TEMPLATE), load_static_template(XU_DISPLAY_TEMPLATE)
    assert set(current) == set(previous)
    assert current['reportSchemaVersion'] == previous['reportSchemaVersion'] == 'gut-barrier-public.v2'
    assert current['overviewSlots'] == previous['overviewSlots']


@pytest.mark.parametrize('score', [0, 3.5, 15, None])
def test_stages_are_concepts_not_an_unfinished_selection_or_inferred_diagnosis(score):
    body = visible_source(_html(_public(score=score)))
    figure = _figure(body, '肠道屏障功能评分与阶段概念')
    assert '健康人群 · 亚健康人群 · 疾病人群' in figure
    assert 'class="overall-stage' not in figure
    assert '本次评估</small>' not in figure
    assert '未配置' not in figure and '68%' not in figure
    if score is not None:
        assert f'{score:g} / 15' in figure


def test_fixed_organ_axis_does_not_depend_on_patient_risk_rows():
    original = _public()
    empty = deepcopy(original)
    next(s for s in empty['sections'] if s['id'] == 'disease_risks')['blocks'] = []
    title = '肠–X 轴：肠道与器官的双向联系'
    first = _figure(visible_source(_html(original)), title)
    second = _figure(visible_source(_html(empty)), title)
    # Contiguous figure IDs legitimately differ when the patient chart is absent.
    normalize = lambda value: re.sub(r'figure-\d+|图 \d+', 'figure', value)
    assert normalize(first) == normalize(second)
    assert all(label in first for label in ('肝脏 · 肠–肝轴', '脑 · 肠–脑轴', '心脏与血管 · 肠–心轴', '胰腺 · 肠–胰轴'))
    assert '方向甲' not in first and '方向乙' not in first


@pytest.mark.parametrize('result,evaluation,abnormal', [
    ('>3.0 mg/L', '超出功能医学标准范围', True),
    ('1.8 mg/L', '超出功能医学标准范围', True),
    ('0 mg/L', '符合功能医学标准值', False),
    ('<1 mg/L', '需核对后评估', False),
    (None, None, False),
    ('', '超出功能医学标准范围', False),
])
def test_indicator_colors_use_existing_definite_evaluation_only(result, evaluation, abnormal):
    report = _public()
    crp = next(row for row in _block(report, '三项检测指标')['rows'] if 'hs-CRP' in row[0])
    crp[1], crp[3] = result, evaluation
    body = visible_source(_html(report))
    marker_table = re.search(r'<h3>1.2 检测项目与结果总览</h3>(.*?)</table>', body, re.S)[1]
    assert ('class="marker-result-abnormal"' in marker_table) is abnormal
    assert ('class="marker-evaluation-abnormal"' in marker_table) is abnormal
    assert '轻度升高' not in _figure(body, '四项检测结果与参考信息')
    assert '中度升高' not in _figure(body, '四项检测结果与参考信息')


def test_fixed_prose_is_concise_ordered_and_safety_qualified():
    asset = load_static_template(REFINED_XU_DISPLAY_TEMPLATE)
    blocks = next(s for s in asset['fixedSections'] if s['id'] == 'repair_plan')['blocks']
    assert len(blocks) == 5 and all(b['kind'] == 'paragraph' for b in blocks)
    assert [b['title'].split()[0] for b in blocks] == ['3.1', '3.2', '3.3', '3.4', '3.5']
    third = '\n'.join(b['text'] for b in blocks)
    assert len(third) < 2500
    assert all(label in blocks[1]['text'] for label in ('一、上皮能量代谢', '二、细胞合成与连接更新', '三、抗氧化防御与黏膜环境'))
    assert all(label in third for label in ('静脉', '肝肾功能', '给药适应证', '供者', '病原体', '免疫抑制', '后生元'))
    assert 'PFMT' not in third and '均显示出较好的临床疗效' not in third
    body = visible_source(_html())
    repair = re.search(r'<section[^>]*id="repair_plan".*?</section>', body, re.S)[0]
    assert repair.index(blocks[0]['text'][:15]) < repair.index('class="principle-grid"') < repair.index('<h3>3.2')
    assert repair.count('class="principle-grid"') == 1
    assert '<ul class="report-list">' not in repair
    assert '5.1 通用质量控制流程' in body
    assert '<h3>健康关注指数标尺说明</h3>' in body


def test_display_does_not_add_static_content_or_aliases_to_callback():
    report = _public()
    before = deepcopy(report)
    rendered = _html(report)
    assert report == before == _embedded(rendered)
    assert not any(s['id'] in ('repair_plan', 'education') for s in _embedded(rendered)['sections'])
    assert '_referenceAliases' not in _embedded(rendered)


def test_known_who_edition_dedup_maps_numeric_and_case_citations_without_changing_fixed_indices():
    report = _public()
    appendix = next(s for s in report['sections'] if s['id'] == 'appendix')
    appendix['blocks'] = [{'kind': 'references', 'title': '本次评估引用', 'items': [
        {'citation': 'WHO guidelines on physical activity and sedentary behaviour',
         'url': 'https://iris.who.int/bitstream/handle/10665/336656/9789240015128-eng.pdf'},
        {'citation': 'Append-only reference', 'url': 'https://example.test/reference-28'},
        {'citation': '[评估7] Case reference', 'url': 'https://example.test/reference-case'},
    ]}]
    overview = next(s for s in report['sections'] if s['id'] == 'overview')
    overview['blocks'].append({'kind': 'paragraph', 'title': '引用定位测试', 'text': 'WHO[补27]、后续来源[补28]、病例依据[评估7]。'})
    before = deepcopy(report)
    view = expand_dynamic_report(report, template_version=REFINED_XU_DISPLAY_TEMPLATE)
    supplementary = _block(view, '营养与微生态干预补充依据')['items']
    assert len(supplementary) == 28
    assert 'WHO guidelines' in supplementary[6]['citation']
    assert supplementary[26]['citation'] == 'Append-only reference'
    assert supplementary[27]['citation'].startswith('[评估7]')
    assert view['_referenceAliases']['supplement-1'][27] == 7
    assert view['_referenceAliases']['supplement-1'][28] == 27
    body = visible_source(_html(report))
    paragraph = re.search(r'<h3>引用定位测试</h3><p>(.*?)</p>', body, re.S)[1]
    assert 'href="#reference-supplement-1-7"' in paragraph
    assert 'href="#reference-supplement-1-27"' in paragraph
    assert 'href="#reference-supplement-1-28">[评估7]' in paragraph
    assert body.count('WHO guidelines on physical activity and sedentary behaviour') == 1
    assert '3.4的IAP与LPS机制' not in body
    assert report == before == _embedded(_html(report))


def test_similar_title_or_named_case_reference_is_not_silently_swallowed():
    report = _public()
    appendix = next(s for s in report['sections'] if s['id'] == 'appendix')
    appendix['blocks'] = [{'kind': 'references', 'title': '本次评估引用', 'items': [
        {'citation': 'WHO guidelines on physical activity and sedentary behaviour', 'url': 'https://example.test/another-edition'},
        {'citation': '[评估9] WHO guidelines on physical activity and sedentary behaviour',
         'url': 'https://iris.who.int/bitstream/handle/10665/336656/9789240015128-eng.pdf'},
    ]}]
    view = expand_dynamic_report(report, template_version=REFINED_XU_DISPLAY_TEMPLATE)
    items = _block(view, '营养与微生态干预补充依据')['items']
    assert len(items) == 28
    assert items[-1]['citation'].startswith('[评估9]')


def test_qualitative_followup_is_not_rendered_as_unassessed_or_zero():
    report = _public()
    rows = _block(report, '2.1 消化系统疾病风险评估')['rows']
    rows.append(['慢性非萎缩性胃炎', None, '随访关注', '既往胃镜已提示胃炎，结合症状随访。'])
    before = deepcopy(report)
    rendered = _html(report)
    figure = _figure(visible_source(rendered), '本次健康关注指数')
    row = next(value for value in re.findall(r'<div class="risk-plot-row">.*?</div>', figure, re.S)
               if '慢性非萎缩性胃炎' in value)
    assert 'class="risk-qualitative">定性随访</span>' in row
    assert '随访关注' in row
    assert '未评估' not in row and '0 / 10' not in row and 'risk-track' not in row
    # Actual zero in another row remains a valid quantitative result.
    assert 'aria-label="方向甲：健康关注指数 0 / 10"' in figure
    assert before == report == _embedded(rendered)
    old_figure = _figure(visible_source(render_report_html(report, template_version=XU_DISPLAY_TEMPLATE)), '本次健康关注指数')
    assert 'risk-qualitative' not in old_figure


def test_v4_print_keeps_figures_together_and_uses_current_footer_title_only_in_v4():
    from adapters.gut_barrier.rendering import _REFINED_CSS
    source = _html()
    old_source = render_report_html(_public(), template_version=XU_DISPLAY_TEMPLATE)
    assert _REFINED_CSS in source and _REFINED_CSS not in old_source
    assert '.report-figure{break-inside:avoid!important;page-break-inside:avoid}' in _REFINED_CSS
    assert '.report-figure figcaption{break-after:avoid;page-break-after:avoid}' in _REFINED_CSS
    assert '@page{@bottom-center{content:"肠道屏障功能与炎症状态评估报告"}}' in _REFINED_CSS
    # A later v4-only rule overrides legacy IDs that opted some figures into splitting.
    assert source.index(_REFINED_CSS) > source.index('#disease_risks .report-figure{break-inside:auto}')
