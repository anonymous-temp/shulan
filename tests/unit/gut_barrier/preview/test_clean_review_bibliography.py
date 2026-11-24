"""v5 filters displayed citations without renumbering or changing callback data."""
from copy import deepcopy
from hashlib import sha256
import json
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import CLEAN_REVIEW_DISPLAY_TEMPLATE
from tests.unit.gut_barrier.preview.support import _report
from tests.unit.gut_barrier.preview.support import visible_source
from tests.unit.gut_barrier.preview.test_xu_rendering import _public, _embedded


def _section(report, identifier):
    return next(section for section in report['sections'] if section['id'] == identifier)


def _reference(title, number):
    return {'citation': f'{title}{number}', 'url': f'https://example.test/{title}/{number}',
            'year': None, 'publisher': None, 'doi': None, 'location': None}


@pytest.fixture
def minimal_asset(monkeypatch):
    # A small independent citation fixture makes numbering assertions unrelated
    # to the production template's particular prose or bibliography size.
    asset = {
        'templateVersion': CLEAN_REVIEW_DISPLAY_TEMPLATE,
        'reportSchemaVersion': 'gut-barrier-public.v2',
        'sectionOrder': ['overview', 'disease_risks', 'repair_plan', 'education', 'appendix'],
        'overviewInsertions': [],
        'overviewSlots': [],
        'diseaseIntroduction': {'kind': 'paragraph', 'title': None, 'text': '合成疾病概览'},
        'diseaseMechanisms': [],
        'fixedSections': [
            {'id': identifier, 'title': title, 'subtitle': None, 'blocks': [], 'visible': True}
            for identifier, title in [('repair_plan', '第三章 干预'), ('education', '第四章 科普')]
        ],
    }
    asset['appendixBlocks'] = [
        {'kind': 'paragraph', 'title': '5.3 免责声明', 'text': '必要边界'},
        {'kind': 'references', 'title': '5.4 参考文献',
         'items': [_reference('主文献', i) for i in range(1, 7)]},
        {'kind': 'references', 'title': '营养与微生态干预补充依据',
         'items': [_reference('补文献', i) for i in range(1, 5)]},
    ]
    monkeypatch.setattr('adapters.gut_barrier.frontend_template.load_static_template', lambda *args, **kwargs: deepcopy(asset))
    return asset


def _case(text='', *, references=()):
    report = _public()
    _section(report, 'overview')['blocks'].append({'kind': 'paragraph', 'title': '实际正文引用', 'text': text})
    _section(report, 'appendix')['blocks'] = [{'kind': 'references', 'title': '5.5 参考文献', 'items': list(references)}]
    return report


def _render(report):
    return render_report_html(report, template_version=CLEAN_REVIEW_DISPLAY_TEMPLATE)


def _anchors(body):
    return set(re.findall(r'id="(reference-[^"]+)"', body))


def _targets(body):
    return set(re.findall(r'href="#(reference-[^"]+)"', body))


def test_v5_keeps_dynamic_notice_once_without_injecting_duplicate_intro(minimal_asset):
    report = _case()
    risk = _section(report, 'disease_risks')
    risk['blocks'].insert(0, deepcopy(minimal_asset['diseaseIntroduction']))
    risk['blocks'].append({'kind': 'paragraph', 'title': '其他临床说明', 'text': '必须保留的个体临床背景。'})
    before = deepcopy(report)
    expanded = expand_dynamic_report(report, template_version=CLEAN_REVIEW_DISPLAY_TEMPLATE)
    blocks = _section(expanded, 'disease_risks')['blocks']
    assert sum(block.get('title') == '本次关注指数' for block in blocks) == 1
    assert minimal_asset['diseaseIntroduction'] not in blocks
    assert not any('本章结合本次可用资料' in (block.get('text') or '') for block in blocks)
    body = visible_source(_render(report))
    assert body.count('<h3>本次关注指数</h3>') == 1
    assert '必须保留的个体临床背景' in body
    assert report == before == _embedded(_render(report))


def test_numeric_ranges_supplement_and_case_citations_keep_original_sparse_numbers(minimal_asset):
    report = _case('主文献[1,4–5]，补充[补2]，病例[评估7]，未知[99]与[评估999]；错误区间[6–4]。',
                   references=[_reference('[评估7]病例来源', 7), _reference('未引用的动态来源', 8)])
    body = visible_source(_render(report))
    assert _anchors(body) == {
        'reference-primary-1', 'reference-primary-4', 'reference-primary-5',
        'reference-supplement-1-2', 'reference-supplement-1-5',
    }
    assert _targets(body) == _anchors(body)
    assert '>4</a>' in body and '>5</a>' in body
    assert '[99]' in body and '[评估999]' in body and '[6–4]' in body
    assert '未引用的动态来源' not in body
    assert '主文献2' not in body and '补文献1' not in body
    assert 'href="#reference-primary-99"' not in body


def test_known_duplicate_publications_keep_alias_mapping_after_filtering(minimal_asset):
    from adapters.gut_barrier.frontend_template import project_display_report
    minimal_asset['appendixBlocks'][-1]['items'][1] = {
        'citation': 'WHO固定来源', 'url': 'https://www.who.int/publications/i/item/9789240015128'}
    report = _case('别名[补5]，后续来源[补6]，命名病例[评估7]。', references=[
        {'citation': 'WHO重复版本', 'url': 'https://iris.who.int/bitstream/handle/10665/336656/9789240015128-eng.pdf'},
        _reference('动态后续来源', 1), _reference('[评估7]病例来源', 7),
    ])
    original = deepcopy(report)
    view = expand_dynamic_report(report, template_version=CLEAN_REVIEW_DISPLAY_TEMPLATE)
    assert view['_referenceAliases']['supplement-1'][5] == 2
    assert view['_referenceAliases']['supplement-1'][6] == 5
    bundle = project_display_report(report, template_version=CLEAN_REVIEW_DISPLAY_TEMPLATE)
    assert bundle['referenceAliases'] == view['_referenceAliases']
    assert bundle['sourceReport'] == original
    body = visible_source(_render(report))
    assert _anchors(body) == {'reference-supplement-1-2', 'reference-supplement-1-5', 'reference-supplement-1-6'}
    assert _targets(body) == _anchors(body)
    assert 'WHO重复版本' not in body
    assert 'href="#reference-supplement-1-6">[评估7]</a>' in body
    assert report == original == _embedded(_render(report))


def test_missing_all_citations_hides_groups_without_new_rules_or_dead_directory_links(minimal_asset):
    report = _case('没有引文的本次结果。', references=[_reference('未引用来源', 1)])
    body = visible_source(_render(report))
    assert not _anchors(body) and not _targets(body)
    assert '<h3>5.4 参考文献</h3>' not in body
    assert 'href="#appendix-5-4"' not in body
    assert '营养与微生态干预补充依据' not in body
    assert '未引述' not in body and '暂无参考文献' not in body
    assert '必要边界' in body
    assert _embedded(_render(report)) == report
    assert _section(report, 'appendix')['blocks'][0]['items']


def test_only_citations_in_actual_compact_display_keep_sources(minimal_asset):
    report = _case('可见来源[1]。')
    overview = _section(report, 'overview')['blocks']
    overview.append({'kind': 'paragraph', 'title': '1.3 肠道屏障功能评分（IBFC）与损伤分级', 'text': '已被视图去重的内容[2]。'})
    overview.append({'kind': 'bands', 'title': '屏障评分分级', 'items': [{'range': '[3]', 'label': '隐藏', 'selected': False}]})
    report['sections'].append({'id': 'hidden', 'visible': False, 'blocks': [{'kind': 'paragraph', 'text': '隐藏章节[4]'}]})
    body = visible_source(_render(report))
    assert _anchors(body) == {'reference-primary-1'}
    assert '已被视图去重' not in body and '隐藏章节' not in body
    assert '主文献2' not in body and '主文献3' not in body and '主文献4' not in body


def test_reference_titles_cannot_self_cite_or_keep_unused_sources(minimal_asset):
    report = _case('', references=[{'citation': '[评估8]标题自引[1][补1]', 'url': 'https://example.test/self'}])
    assert not _anchors(visible_source(_render(report)))


def test_case_names_for_same_publication_remain_resolvable_and_safe(minimal_asset):
    report = _case('两个病例来源[评估7][评估8]；未知危险内容<img src=x onerror=alert(1)>[999]。', references=[
        {'citation': '[评估7]<script>first</script>', 'url': 'javascript:alert(1)'},
        {'citation': '[评估8]second', 'url': 'https://example.test/same'},
        {'citation': '[评估9]uncited', 'url': 'https://example.test/same'},
    ])
    body = visible_source(_render(report))
    assert _targets(body) == {'reference-supplement-1-5', 'reference-supplement-1-6'}
    assert _targets(body) == _anchors(body)
    assert '<script>first' not in body and '<img src=' not in body
    assert '&lt;script&gt;first' in body and '&lt;img src=' in body
    assert 'javascript:' not in body and 'reference-primary-999' not in body
    assert '[评估9]uncited' not in body


@pytest.mark.parametrize('citation, expected', [
    ('[评估16] Guidelines for chronic gastritis', 'Guidelines for chronic gastritis'),
    ('[评估16]胃炎诊疗指南', '胃炎诊疗指南'),
    ('[评估16] <img src=x onerror=alert(1)>', '&lt;img src=x onerror=alert(1)&gt;'),
    ('[评估16]', '参考资料'),
])
def test_v5_bibliography_removes_named_prefix_but_keeps_inline_link_and_source(minimal_asset, citation, expected):
    report = _case('本次依据[评估16]。', references=[{
        'citation': citation, 'url': 'https://example.test/gastritis', 'year': 2023,
        'publisher': 'Journal', 'doi': '10.test/gastritis', 'location': '15–20',
    }])
    original = deepcopy(report)
    rendered = _render(report)
    body = visible_source(rendered)
    entry = re.search(r'<li id="reference-supplement-1-5">(.*?)</li>', body, re.S)[1]
    assert '[补5]</span>' in entry
    assert expected in entry
    assert '[评估16]' not in entry
    assert '<img' not in entry
    assert '2023 · Journal · 10.test/gastritis · 15–20' in entry
    assert 'href="#reference-supplement-1-5">[评估16]</a>' in body
    assert _targets(body) == _anchors(body) == {'reference-supplement-1-5'}
    assert report == original == _embedded(rendered)


@pytest.mark.parametrize('version', ['v1', 'v2', 'v3', 'v4'])
def test_historical_bibliography_keeps_original_named_title(minimal_asset, version):
    report = _case('历史引用[评估16]。', references=[{
        'citation': '[评估16] Historical title', 'url': 'https://example.test/history'}])
    body = visible_source(render_report_html(report, template_version=f'gut-barrier-static.{version}'))
    entry = re.search(r'<li id="reference-supplement-1-5">(.*?)</li>', body, re.S)[1]
    assert '[补5]</span>[评估16] Historical title' in entry
    assert 'href="#reference-supplement-1-5">[评估16]</a>' in body


def test_source_scope_dynamic_paragraph_is_visible_before_case_conclusion(minimal_asset):
    report = _case()
    _section(report, 'overview')['blocks'].append({
        'kind': 'paragraph', 'title': '本次评估资料范围', 'text': '一份外部资料未采用，缺少身份核对信息。'})
    before = deepcopy(report)
    rendered = _render(report)
    body = visible_source(rendered)
    assert body.index('<h3>本次评估资料范围</h3>') < body.index('<h3>1.1 综合评估指数')
    assert '一份外部资料未采用' in body
    assert report == before == _embedded(rendered)


@pytest.mark.parametrize('version,digest', [
    ('v1', '6cbe9851b47ddff0ba117e494b815d330bac372b2abc3da07e0deb2f25176732'),
    ('v2', 'cf6a389bd3c35a0620bd3a5c00d91718ef21affe7775373382ccc0ad0eeb557a'),
    ('v3', '447c6fe7e74269e4c30a5ed25c16628ffff43bb1d5db5eb2953cc65126261ac0'),
    ('v4', 'd6eb413e68e77993158f146a76a9e106357cdfa5d4035f55a71a808db9f662e2'),
])
def test_old_releases_are_byte_identical(version, digest, historical_templates):
    result = render_report_html(_report(), template_version=f'gut-barrier-static.{version}')
    assert sha256(result.encode()).hexdigest() == digest
