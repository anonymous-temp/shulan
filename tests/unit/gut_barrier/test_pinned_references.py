"""Requested bibliography order never invents or moves a clinical source claim."""
from copy import deepcopy
import json
import re

import pytest

from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.reference_order import (
    PRIMARY_TITLE, REFERENCE_FIELDS, bind_ordered_static_copy, load_reference_asset,
    publish_ordered_references, reference_keys,
)
from modules.gut_barrier.reference_pins import pinned_references, pinned_reference_audit
from tests.unit.gut_barrier.test_ordered_references import fixture, references
from tests.unit.gut_barrier.test_reference_budget import _budget_fixture
from tests.unit.gut_barrier.test_product_display_feedback import _source


def publish(report, source, asset, audit=None, limit=15):
    return publish_ordered_references(
        report, source, asset, reference_limit=limit, reference_audit=audit,
        pinned_references=pinned_references(), allow_fixed_budget_omission=True)


def test_reviewed_prefix_is_li_then_original_zip_order_with_field_provenance():
    pinned = pinned_references()
    assert len(pinned) == 8
    assert [item['doi'] for item in pinned] == [
        '10.13381/j.cnki.cjm.2009.01.018', '10.3760/cma.j.cn311367-20201116-00660',
        '10.13381/j.cnki.cjm.201606001', '10.3760/cma.j.cn112137-20240815-01881',
        '10.3760/cma.j.issn.1674-635X.2019.04.001', '10.3760/cma.j.cn371439-20210202-00027',
        '10.3760/cma.j.cn311367-20210930-00524', '10.3760/cma.j.cn.441530-20200420-00231',
    ]
    assert all(set(item) == set(REFERENCE_FIELDS) for item in pinned)
    assert '医学营养治疗指南（2025版）' in pinned[3]['citation']
    audit = pinned_reference_audit()
    assert audit['sources'][0]['provenance']['userProvidedFields'] == ['doi', 'volume', 'issue', 'pages']
    assert [item['provenance']['archiveOrder'] for item in audit['sources'][1:]] == list(range(1, 8))
    pinned[0]['citation'] = 'mutated'
    audit['sources'][0]['provenance'].clear()
    assert pinned_references()[0]['citation'].startswith('李兰娟.')
    assert pinned_reference_audit()['sources'][0]['provenance']['evimedId']


def test_fixed_prefix_and_existing_risk_priority_share_fifteen_slots_without_fake_citations():
    report, source, asset = _budget_fixture()
    before, audit = deepcopy(source), {}
    publish(report, source, asset, audit)
    visible = references(report, PRIMARY_TITLE)
    assert len(visible) == 15 and visible[:8] == pinned_references()
    assert {'Scoped source 22', 'Scoped source 23', 'Scoped source 24'} <= {
        item['citation'] for item in visible[8:]}
    rows = report['sections'][0]['blocks'][0]['rows']
    original = before['sections'][0]['blocks'][0]['rows']
    assert [row[:3] for row in rows] == [row[:3] for row in original]
    assert [re.sub(r'\[.*?\]', '', row[3]) for row in rows] == [
        re.sub(r'\[.*?\]', '', row[3]) for row in original]
    numbers = [int(n) for row in rows for n in re.findall(r'\[(\d+)\]', row[3])]
    assert list(dict.fromkeys(numbers)) == list(range(9, 16))
    ledger = audit['referencePublication']
    assert ledger['pinnedCount'] == 8 and ledger['fixedRequiredCount'] == 0
    assert len(ledger['claims']) == 24 and source == before
    assert len([item for item in ledger['inventory'] if item['selection'] == 'pinned']) == 8
    assert all(not set(claim['selectedBodyNumbers']) & set(range(1, 9)) for claim in ledger['claims'])


def test_exact_doi_alias_deduplicates_and_maps_existing_claim_to_pinned_number():
    report, source, asset = _budget_fixture()
    first = references(report, PRIMARY_TITLE)[0]
    first.update(citation='Alternate short citation', url='https://doi.org/10.13381/J.CNKI.CJM.2009.01.018',
                 doi='DOI:10.13381/j.cnki.cjm.2009.01.018')
    audit = {}
    publish(report, source, asset, audit)
    visible = references(report, PRIMARY_TITLE)
    assert len(visible) == 15 and visible[:8] == pinned_references()
    assert report['sections'][0]['blocks'][0]['rows'][0][3] == 'Mechanism 1.[1]'
    assert sum(bool(reference_keys(item) & reference_keys(visible[0])) for item in visible) == 1
    assert audit['referencePublication']['claims'][0]['selectedBodyNumbers'] == [1]


def test_insufficient_real_sources_are_audited_without_padding_or_invented_claims():
    report, source, asset = _budget_fixture(1)
    audit = {}
    publish(report, source, asset, audit)
    assert len(references(report, PRIMARY_TITLE)) == 9
    assert audit['referencePublication']['belowTargetReason'] == 'insufficient_distinct_verified_sources'
    assert len(audit['referencePublication']['claims']) == 1


def test_pin_budget_overflow_fails_before_mutation():
    report, source, asset = fixture()
    original = deepcopy(report)
    with pytest.raises(ValueError, match='exceed reference display limit'):
        publish(report, source, asset, limit=7)
    assert report == original


def test_current_release_preserves_full_static_source_audit_and_bound_claims():
    source = _source()
    source['meta']['displayTemplateVersion'] = LATEST_DISPLAY_TEMPLATE
    audit = {}
    report = project_callback_report(source, reference_audit=audit)
    visible = references(report, PRIMARY_TITLE)
    assert len(visible) == 15 and visible[:8] == pinned_references()
    ledger = audit['referencePublication']
    assert ledger['fixedCitedCount'] == 8 and ledger['fixedRequiredCount'] == 0
    omitted = [claim for claim in ledger['claims'] if claim['origin'] == 'fixed'
               and len(claim['selectedBodyNumbers']) < len(claim['sourceRoots'])]
    assert omitted and all(claim['text'] and claim['sourceRoots'] for claim in omitted)
    asset = load_reference_asset(LATEST_DISPLAY_TEMPLATE)
    original = deepcopy(asset)
    bound = bind_ordered_static_copy(asset, report, allow_omitted_fixed=True)
    assert asset == original
    for claim in ledger['claims']:
        if claim['origin'] != 'fixed':
            continue
        text = next(block['text'] for section in bound['fixedSections'] for block in section['blocks']
                    if block.get('title') == claim['blockTitle'])
        assert re.sub(r'\[(?:补)?[\d,]+\]', '', text) == re.sub(r'\[(?:补)?[\d,]+\]', '', claim['text'])
        numbers = [int(n) for group in re.findall(r'\[([\d,]+)\]', text) for n in group.split(',')]
        assert list(dict.fromkeys(numbers)) == claim['selectedBodyNumbers']
    assert 'pinnedCatalog' not in json.dumps(report, ensure_ascii=False)


def test_latest_release_rejects_missing_or_reordered_frozen_prefix_instead_of_repairing_it():
    source = _source()
    source['meta']['displayTemplateVersion'] = LATEST_DISPLAY_TEMPLATE
    report = project_callback_report(source)
    items = references(report, PRIMARY_TITLE)
    items[0], items[1] = items[1], items[0]
    with pytest.raises(ValueError, match='pinned reference prefix'):
        bind_ordered_static_copy(load_reference_asset(LATEST_DISPLAY_TEMPLATE), report,
                                 allow_omitted_fixed=True)


def test_frozen_receipt_and_release_binding_do_not_consult_current_pinned_catalog(monkeypatch):
    from modules.gut_barrier import reference_pins
    source = _source()
    source['meta']['displayTemplateVersion'] = LATEST_DISPLAY_TEMPLATE
    report = project_callback_report(source)
    before = deepcopy(report)
    def forbidden():
        raise AssertionError('Frozen receipts must not reload current pins')
    monkeypatch.setattr(reference_pins, 'pinned_references', forbidden)
    audit = {}
    assert project_callback_report(report, reference_audit=audit) == before and audit == {}
    bind_ordered_static_copy(load_reference_asset(LATEST_DISPLAY_TEMPLATE), report,
                             allow_omitted_fixed=True)
    assert report == before
