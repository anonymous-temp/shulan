from adapters.gut_barrier import frontend_template as _preview_assets_module
from pathlib import Path as _PreviewPath
PREVIEW_ASSETS = _PreviewPath(_preview_assets_module.__file__).parent / "assets"
"""v6 changes the view, never the frozen callback or historical releases."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report, load_static_template
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    CLEAN_REVIEW_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE, SOURCE_STAGE_DISPLAY_TEMPLATE,
    display_capabilities, resolve_display_template,
)
from modules.gut_barrier.publication import project_callback_report
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report
from tests.unit.gut_barrier.preview.support import _report
from tests.unit.gut_barrier.preview.archive_support import historical_asset

ASSETS = PREVIEW_ASSETS
HISTORICAL = {
    'v1': ('62b4a6ebc0b9f0c7c8e4b09e05bfeaebbb876bd3e20431bcf9b41e18983ce9a0',
           '6cbe9851b47ddff0ba117e494b815d330bac372b2abc3da07e0deb2f25176732'),
    'v2': ('5162b8268c709db333b3a76e4e52daf75ef6e0db01db203102f14243e056036c',
           'cf6a389bd3c35a0620bd3a5c00d91718ef21affe7775373382ccc0ad0eeb557a'),
    'v3': ('e8cfb540c5398b1b26c1b089c27660b2b2674d39c08dfa897ead4f4002bb705c',
           '447c6fe7e74269e4c30a5ed25c16628ffff43bb1d5db5eb2953cc65126261ac0'),
    'v4': ('d54b46dbd95d6d305aa4d62eb2d1a1ac7e5830e23d6ce68962f45de64851697d',
           'd6eb413e68e77993158f146a76a9e106357cdfa5d4035f55a71a808db9f662e2'),
    'v5': ('74dc75a59e6c44d9e4c28ffd4fb6e00c0d6e78f80b88ca5d1ed4a7087820ae06',
           'ede6191e64e0375533027003cda7fedc39e9805410c18ee2fb3ed33605cde51a'),
}


def embedded(source):
    return json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', source, re.S)[1])


def test_source_stage_capability_is_explicit_and_unbound_receipts_remain_legacy():
    assert SOURCE_STAGE_DISPLAY_TEMPLATE == 'gut-barrier-static.v6'
    assert LATEST_DISPLAY_TEMPLATE == 'gut-barrier-static.v14'
    current = display_capabilities(SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert current.source_stage_layout
    assert current.compact_layout and current.xu_review and current.refined_fixed_layout and current.clean_review_layout
    for version in HISTORICAL:
        assert not display_capabilities(f'gut-barrier-static.{version}').source_stage_layout
    callback_report = project_callback_report(clinical_report())
    callback_report['meta']['displayTemplateVersion'] = SOURCE_STAGE_DISPLAY_TEMPLATE
    assert resolve_display_template(callback_report) == 'gut-barrier-static.v1'
    assert resolve_display_template(callback_report, SOURCE_STAGE_DISPLAY_TEMPLATE) == SOURCE_STAGE_DISPLAY_TEMPLATE


def test_v6_static_asset_keeps_same_wire_shape_and_patient_independent_content(historical_templates):
    old = load_static_template(CLEAN_REVIEW_DISPLAY_TEMPLATE)
    current = load_static_template(SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert set(current) == set(old)
    assert current['templateVersion'] == SOURCE_STAGE_DISPLAY_TEMPLATE
    assert current['reportSchemaVersion'] == old['reportSchemaVersion'] == 'gut-barrier-public.v2'
    for key in set(old) - {'templateVersion', 'sourceVersions', 'overviewSlots'}:
        assert current[key] == old[key]
    assert current['overviewSlots'] == [slot for slot in old['overviewSlots']
        if not (slot['kind'] == 'cards' and slot['itemLabel'] == 'IBFC评分' and slot['field'] == 'detail')]


def test_current_asset_is_patient_independent_and_loads_without_a_historical_archive(monkeypatch):
    monkeypatch.setattr('adapters.gut_barrier.frontend_template._archived_template_path',
                        lambda *args: pytest.fail('the latest template must not consult historical archives'))
    current = load_static_template(LATEST_DISPLAY_TEMPLATE)
    assert current['templateVersion'] == LATEST_DISPLAY_TEMPLATE
    assert current['reportSchemaVersion'] == 'gut-barrier-public.v2'
    assert current['sectionOrder'] == ['overview', 'disease_risks', 'repair_plan', 'appendix']
    assert [section['id'] for section in current['fixedSections']] == ['repair_plan']
    source = json.dumps(current, ensure_ascii=False)
    for private in ('"subject"', '"riskIndex"', '"sessionId"', 'acceptance-user', 'gbr_new_test'):
        assert private not in source


def test_v6_retains_unknown_fields_safety_text_and_meaningful_zero_false(historical_templates):
    report = project_callback_report(clinical_report(pattern='---'))
    overview = next(section for section in report['sections'] if section['id'] == 'overview')
    overview['blocks'].extend([
        {'kind': 'facts', 'title': '新增检查', 'items': [
            {'label': '确切零值', 'value': 0}, {'label': '明确否定', 'value': False}]},
        {'kind': 'paragraph', 'title': '个体安全信息', 'text': '<script>UNTRUSTED</script>'},
    ])
    before = deepcopy(report)
    html = render_report_html(report, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)
    visible = html.split('<script type="application/json"')[0]
    assert '确切零值' in visible and '>0</div>' in visible
    assert '明确否定' in visible and '>False</div>' in visible
    assert '&lt;script&gt;UNTRUSTED&lt;/script&gt;' in visible
    assert '<script>UNTRUSTED</script>' not in visible
    assert embedded(html) == before == report


def test_v6_expansion_does_not_overwrite_patient_risk_values_or_unknown_table_rows(historical_templates):
    report = project_callback_report(clinical_report())
    disease = next(section for section in report['sections'] if section['id'] == 'disease_risks')
    custom = {'kind': 'table', 'title': '额外检查资料', 'columns': ['名称', '结论', '备注'],
              'rows': [['未知项目', 0, False], [['未知结构'], None, '待补充']]}
    disease['blocks'].append(custom)
    before = deepcopy(report)
    expanded = expand_dynamic_report(report, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert custom in next(section for section in expanded['sections'] if section['id'] == 'disease_risks')['blocks']
    assert expand_dynamic_report(expanded, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)['sections'] == expanded['sections']
    assert report == before


@pytest.mark.parametrize('version', HISTORICAL)
def test_v1_to_v5_assets_and_rendered_fixture_remain_byte_identical(version, historical_templates):
    asset_digest, html_digest = HISTORICAL[version]
    asset = historical_asset(version)
    assert sha256(asset.read_bytes()).hexdigest() == asset_digest
    rendered = render_report_html(_report(), template_version=f'gut-barrier-static.{version}',
                                  template_archive_dir=asset.parent)
    assert sha256(rendered.encode()).hexdigest() == html_digest


@pytest.mark.parametrize('pattern', ('+--', '-+-', '--+', '++-', '+-+', '-++', '+++', '---'))
def test_all_patterns_remain_identical_dynamic_json_after_v6_expansion_and_rendering(pattern, historical_templates):
    callback_report = project_callback_report(clinical_report(pattern=pattern))
    original = deepcopy(callback_report)
    expanded = expand_dynamic_report(callback_report, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert callback_report == original
    assert [section['id'] for section in expanded['sections']] == [
        'overview', 'disease_risks', 'repair_plan', 'education', 'appendix']
    rendered = render_report_html(callback_report, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert embedded(rendered) == callback_report == original
    assert callback_report['schemaVersion'] == 'gut-barrier-public.v2'
    assert set(callback_report) == {'schemaVersion', 'meta', 'sections'}
    assert set(callback_report['meta']) == {'title', 'reportId'}


@pytest.mark.parametrize('kwargs', [{'missing': True}, {'age': 17}, {'age': None}, {'crp': '>3.0'}, {'crp': 12}])
def test_v6_does_not_drop_unknown_age_range_or_missing_data_from_callback(kwargs, historical_templates):
    callback_report = project_callback_report(clinical_report(**kwargs))
    original = deepcopy(callback_report)
    rendered = render_report_html(callback_report, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert embedded(rendered) == original == callback_report
    assert '[object Object]' not in rendered


def test_rendering_v6_does_not_reassess_or_create_overall_stage(monkeypatch, historical_templates):
    callback_report = project_callback_report(clinical_report(pattern='---'))
    before = deepcopy(callback_report)
    monkeypatch.setattr('modules.gut_barrier.assessment.assess_case',
                        lambda *args, **kwargs: pytest.fail('display must not reassess'))
    monkeypatch.setattr('modules.gut_barrier.rules.score_marker',
                        lambda *args, **kwargs: pytest.fail('display must not score markers'))
    monkeypatch.setattr('modules.gut_barrier.rules.grade_score',
                        lambda *args, **kwargs: pytest.fail('display must not grade scores'))
    rendered = render_report_html(callback_report, template_version=SOURCE_STAGE_DISPLAY_TEMPLATE)
    assert embedded(rendered) == before == callback_report
