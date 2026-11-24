"""Source limitations stay auditable without reaching new public callbacks."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.source_scope import source_scope_block
from modules.gut_barrier.dynamic_diseases import _reference, _public_reference
from tests.unit.gut_barrier.test_xu_review_release import request


def test_no_limitations_do_not_create_a_generic_warning():
    assert source_scope_block([], []) is None
    assert source_scope_block([{'code': 'EVIDENCE_PRIMARY_EMPTY_OR_UNSUITABLE'}], []) is None


def test_retired_identity_codes_do_not_create_patient_identity_restrictions():
    issues = [dict(code='HEALTH_SURVEY_IDENTITY_CONFLICT', field=field,
                   value='Other patient secret', url='https://secret.test/token')
              for field in ('name', 'age', 'gender')]
    issues.append({'code': 'SOURCE_IDENTITY_CONFLICT', 'reason': 'private'})
    before = deepcopy(issues)
    block = source_scope_block(issues, [])
    assert block is None
    assert issues == before


@pytest.mark.parametrize('code', ['SOURCE_UNAVAILABLE', 'SOURCE_COLLECTION_UNAVAILABLE',
    'OCR_UNAVAILABLE', 'OCR_PAGE_LIMIT_REACHED', 'OCR_BUDGET_EXHAUSTED',
    'OCR_CAPACITY_EXHAUSTED', 'SOURCE_PAGE_LIMIT_EXCEEDED', 'SOURCES_SKIPPED_OFFLINE',
    'UNRECOGNIZED_SOURCE_CONTENT'])
def test_unreadable_sources_do_not_claim_complete_exclusion_of_usable_pages(code):
    block = source_scope_block([{'code': code, 'errorType': 'sensitive'}], [])
    assert '未能取得完整的可用内容' in block['text']
    assert '全部附件' not in block['text'] and 'sensitive' not in block['text']


def test_failed_page_summary_is_also_a_source_limit():
    block = source_scope_block([], [{'status': 'parsed', 'failedPages': [3]}])
    assert '未能取得完整的可用内容' in block['text']


def test_conflicting_microbiome_metrics_not_all_microbiome_data_are_excluded():
    block = source_scope_block([{'code': 'AUXILIARY_METRIC_CONFLICT'}], [])
    assert '不一致的菌群指标' in block['text']
    assert '全部菌群' not in block['text']


@pytest.mark.parametrize('key', [f'Q{i}' for i in (1, 2, 3, 4, 12, 15, 16, 17, 18)])
def test_patient_bibliography_keeps_keys_but_not_internal_scope_notes(key):
    internal = _reference(key)
    public = _public_reference(key)
    assert internal['location'] and public['location'] is None
    assert set(internal) == set(public)
    assert {k: v for k, v in internal.items() if k != 'location'} == {
        k: v for k, v in public.items() if k != 'location'}


@pytest.mark.asyncio
async def test_pipeline_keeps_scope_internally_but_omits_it_from_new_callback():
    sources = SimpleNamespace(collect=AsyncMock(return_value={
        'observations': [], 'sources': [], 'context': {},
        'issues': [{'code': 'OCR_PAGE_LIMIT_REACHED'}]}))
    document, audit = await GutBarrierPipeline(sources=sources).generate(
        request('>3.0'), report_id='scope-test', evaluation_date='2026-09-28')
    public = project_callback_report(document)
    blocks = next(s['blocks'] for s in public['sections'] if s['id'] == 'overview')
    scope = [b for b in blocks if b['title'] == '本次评估资料范围']
    assert scope == []
    internal = next(s['blocks'] for s in document['sections'] if s['id'] == 'overview')
    original = next(b for b in internal if b['title'] == '本次评估资料范围')
    assert set(original) == {'kind', 'title', 'text'}
    assert audit['issues'] == [{'code': 'OCR_PAGE_LIMIT_REACHED'}]
    assert audit['partial'] is True
    assert public == project_callback_report(public)
    assert all(s['id'] not in {'repair_plan', 'education'} for s in public['sections'])
    assert '>3.0' not in json.dumps(public, ensure_ascii=False)


@pytest.mark.asyncio
async def test_source_adapter_exception_stays_internal_without_public_error_details():
    sources = SimpleNamespace(collect=AsyncMock(side_effect=RuntimeError('token=private')))
    document, audit = await GutBarrierPipeline(sources=sources).generate(
        request(), report_id='scope-failure', evaluation_date='2026-09-28')
    public = project_callback_report(document)
    text = json.dumps(public, ensure_ascii=False)
    assert '本次评估资料范围' not in text and 'token=private' not in text
    assert audit['partial'] is True
    assert any(item['code'] == 'SOURCE_COLLECTION_UNAVAILABLE' for item in audit['issues'])
    assert any(b['title'] == '本次评估资料范围' for s in document['sections'] for b in s['blocks'])
