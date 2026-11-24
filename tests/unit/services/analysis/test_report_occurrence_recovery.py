"""Stable v6 source occurrences survive metadata recovery without duplicate facts."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import DuplicateKeyError

from health_profile.models import FactStatus
from health_profile.report_ingestion import HealthReportProfileProcessor
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import extended_flow


class UpdatingRepository(FakeProfileRepository):
    async def create_fact(self, fact):
        await asyncio.sleep(0)
        if fact.fact_id in getattr(self, 'tombstones', {}):
            raise DuplicateKeyError('deleted fact ID remains unique')
        return await super().create_fact(fact)

    async def get_deleted_fact_tombstone(self, user_id, subject_id, fact_id):
        return getattr(self, 'tombstones', {}).get(fact_id)

    async def replace_fact(self, fact, *, expected_version, allowed_statuses):
        await asyncio.sleep(0)
        for index, stored in enumerate(self.facts):
            if (stored.user_id, stored.subject_id, stored.fact_id) != (fact.user_id, fact.subject_id, fact.fact_id):
                continue
            if stored.version != expected_version or stored.status not in allowed_statuses:
                return None
            self.facts[index] = fact
            return fact
        return None


class RacingCollection(MemoryMongoCollection):
    async def update_one(self, filters, update):
        await asyncio.sleep(0)
        return await super().update_one(filters, update)


def bound_result(*, unit=None, flag=None, date='2026-09-01', status='partial', key='row-one'):
    return {
        'reportType': 'lab',
        'reportTime': date,
        'status': 'pending',
        'observations': [{'itemName': '尿酸', 'resultValue': '468', 'unit': unit, 'flag': flag}],
        'processing': {
            'status': status,
            'retryable': status != 'complete',
            'issueCount': 1,
            'issues': [{'code': 'optional_evidence_missing', 'sourceIds': ['private-row'], 'path': '$.row'}],
        },
        'sourceBindings': {'/observations/0': f'v6:{key}'},
    }


async def initial_flow(first):
    flow = extended_flow()
    flow.observations.collection = RacingCollection()
    flow.interpreter.analyze = AsyncMock(return_value=deepcopy(first))
    outcome = await flow.service.process_document(**request())
    flow.repository = UpdatingRepository(ReportIngestionFixture.subject())
    flow.processor = HealthReportProfileProcessor(store=flow.reports, repository=flow.repository)
    return flow, outcome


async def save_revision(flow, outcome, key, result):
    return await flow.source.save_revision(
        canonical_report_id=outcome.canonical_report_id,
        user_id='user-1',
        subject_id='subject-self',
        source_key=key,
        result=result,
    )


@pytest.mark.asyncio
async def test_same_source_unit_and_flag_enrich_one_observation_and_one_fact():
    flow, outcome = await initial_flow(bound_result())
    original = await flow.reports.get_report(outcome.canonical_report_id)
    first_job = next(iter(flow.jobs.jobs.values()))
    await flow.processor.process(first_job)
    first_fact_id = flow.repository.facts[0].fact_id
    saved = await save_revision(
        flow, outcome, 'metadata-v2', bound_result(unit='umol/L', flag='超标', status='complete')
    )
    job = next(job for job in flow.jobs.jobs.values() if job.source_revision == saved.record.source_revision)
    await flow.processor.process(job)
    assert len(flow.repository.facts) == 1
    fact = flow.repository.facts[0]
    assert fact.fact_id == first_fact_id and fact.value['unit'] == 'umol/L' and fact.interpretation == 'high'
    assert fact.version == 2
    rows = (await flow.observations.list_observations('user-1', 'subject-self'))['items']
    assert len(rows) == 1 and rows[0]['observation']['unit'] == 'umol/L'
    assert (await flow.reports.get_report(outcome.canonical_report_id)).result == original.result
    assert any(event.event_type == 'fact_enriched' for event in flow.repository.events)
    assert 'sourceBindings' not in outcome.report
    assert 'issues' not in outcome.report['processing']
    assert 'sourceBindings' in original.result


@pytest.mark.asyncio
async def test_late_original_job_never_downgrades_enriched_fact():
    flow, outcome = await initial_flow(bound_result())
    initial_job = next(iter(flow.jobs.jobs.values()))
    saved = await save_revision(flow, outcome, 'metadata-v2', bound_result(unit='umol/L', flag='偏高'))
    new_job = next(job for job in flow.jobs.jobs.values() if job.source_revision == saved.record.source_revision)
    await flow.processor.process(new_job)
    await flow.processor.process(initial_job)
    await flow.processor.process(new_job)
    assert len(flow.repository.facts) == 1
    assert flow.repository.facts[0].value['unit'] == 'umol/L'
    assert flow.repository.facts[0].interpretation == 'high'
    # An old canonical read also replays capture, and must not downgrade the source projection.
    await flow.source.save(
        report_id=outcome.canonical_report_id,
        user_id='user-1',
        subject_id='subject-self',
        source_fingerprint=(await flow.reports.get_report(outcome.canonical_report_id)).source_fingerprint,
        result=bound_result(),
        document_id=outcome.document_id,
        canonical_report_id=outcome.canonical_report_id,
    )
    assert len(flow.observations.collection.documents) == 1
    assert next(iter(flow.observations.collection.documents.values()))['observation']['unit'] == 'umol/L'


@pytest.mark.asyncio
async def test_missing_date_becomes_one_dated_observation_and_one_fact():
    flow, outcome = await initial_flow(bound_result(date=None))
    original_job = next(iter(flow.jobs.jobs.values()))
    await flow.processor.process(original_job)
    assert not flow.repository.facts
    saved = await save_revision(flow, outcome, 'date-v2', bound_result(unit='umol/L'))
    job = next(job for job in flow.jobs.jobs.values() if job.source_revision == saved.record.source_revision)
    await flow.processor.process(job)
    assert len(flow.repository.facts) == len(flow.observations.collection.documents) == 1
    assert flow.repository.facts[0].effective_period.start.day == 1
    row = next(iter(flow.observations.collection.documents.values()))
    assert row['dateStatus'] == 'known'


@pytest.mark.asyncio
async def test_distinct_physical_rows_keep_equal_values_on_different_dates_separate():
    first = bound_result(unit='umol/L', date=None, status='complete')
    first['observations'] = [
        {**first['observations'][0], 'observedAt': '2026-09-01'},
        {**first['observations'][0], 'observedAt': '2026-09-02'},
    ]
    first['sourceBindings']['/observations/1'] = 'v6:row-two'
    flow, _ = await initial_flow(first)
    await flow.processor.process(next(iter(flow.jobs.jobs.values())))
    assert len(flow.observations.collection.documents) == len(flow.repository.facts) == 2
    assert {fact.effective_period.start.day for fact in flow.repository.facts} == {1, 2}


@pytest.mark.asyncio
async def test_concurrent_complementary_metadata_is_merged_without_duplicate_fact():
    flow, outcome = await initial_flow(bound_result())
    await flow.processor.process(next(iter(flow.jobs.jobs.values())))
    a, b = await asyncio.gather(
        save_revision(flow, outcome, 'units', bound_result(unit='umol/L')),
        save_revision(flow, outcome, 'flag', bound_result(flag='偏高')),
    )
    jobs = [
        job
        for job in flow.jobs.jobs.values()
        if job.source_revision in {a.record.source_revision, b.record.source_revision}
    ]
    await asyncio.gather(*(flow.processor.process(job) for job in jobs))
    assert len(flow.repository.facts) == len(flow.observations.collection.documents) == 1
    fact = flow.repository.facts[0]
    assert fact.value['unit'] == 'umol/L' and fact.interpretation == 'high'
    row = next(iter(flow.observations.collection.documents.values()))
    assert row['observation']['unit'] == 'umol/L' and row['observation']['flag'] == '偏高'


@pytest.mark.asyncio
async def test_late_conflicting_metadata_or_core_value_cannot_replace_confirmed_source():
    flow, outcome = await initial_flow(bound_result(unit='umol/L', flag='偏高'))
    await flow.processor.process(next(iter(flow.jobs.jobs.values())))
    old = bound_result(unit='mg/dL', flag='偏低', date='2026-09-02')
    await save_revision(flow, outcome, 'conflicting-metadata', old)
    old['observations'][0]['resultValue'] = '999'
    await save_revision(flow, outcome, 'conflicting-core', old)
    for job in flow.jobs.jobs.values():
        await flow.processor.process(job)
    assert len(flow.repository.facts) == len(flow.observations.collection.documents) == 1
    row = next(iter(flow.observations.collection.documents.values()))
    assert row['observation']['resultValue'] == '468'
    assert row['observation']['unit'] == 'umol/L' and row['observation']['flag'] == '偏高'
    assert row['observedAt'].startswith('2026-09-01')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [FactStatus.RETRACTED, FactStatus.ACTIVE])
async def test_metadata_recovery_preserves_user_modified_or_retracted_facts(status):
    flow, outcome = await initial_flow(bound_result())
    await flow.processor.process(next(iter(flow.jobs.jobs.values())))
    protected = flow.repository.facts[0].model_copy(update={'status': status, 'last_request_id': 'manual-review'})
    flow.repository.facts[0] = protected
    saved = await save_revision(flow, outcome, 'units', bound_result(unit='umol/L', flag='偏高'))
    job = next(job for job in flow.jobs.jobs.values() if job.source_revision == saved.record.source_revision)
    await flow.processor.process(job)
    assert len(flow.repository.facts) == 1 and flow.repository.facts[0] == protected


@pytest.mark.asyncio
async def test_metadata_recovery_and_late_job_cannot_recreate_deleted_fact():
    flow, outcome = await initial_flow(bound_result())
    original_job = next(iter(flow.jobs.jobs.values()))
    await flow.processor.process(original_job)
    fact_id = flow.repository.facts[0].fact_id
    flow.repository.facts = []
    flow.repository.tombstones = {fact_id: {'factId': fact_id, 'status': 'deleted'}}
    saved = await save_revision(flow, outcome, 'units', bound_result(unit='umol/L', flag='偏高'))
    job = next(job for job in flow.jobs.jobs.values() if job.source_revision == saved.record.source_revision)
    await flow.processor.process(job)
    await flow.processor.process(original_job)
    assert not flow.repository.facts


@pytest.mark.parametrize('case', [
    'same_record', 'new_binding', 'ambiguous_history', 'ambiguous_current', 'different_date',
])
def test_json_results_only_inherit_unambiguous_existing_storage_bindings(case):
    from copy import deepcopy

    from modules.analysis.application.document_reuse import _inherit_published_bindings

    item = {'itemName': '指标甲', 'resultValue': '1', 'unit': None}
    previous = {
        'reportType': 'lab',
        'reportTime': '2026-01-01',
        'observations': [dict(item)],
        'sourceBindings': {'/observations/0': 'v6:old'},
    }
    current = {'reportType': 'lab', 'reportTime': '2026-01-01', 'observations': [dict(item)]}
    expected = {'/observations/0': 'v6:old'}
    if case == 'new_binding':
        current['sourceBindings'] = {'/observations/0': 'v6:current'}
        expected = current['sourceBindings']
    elif case == 'ambiguous_history':
        previous['observations'].append(dict(item))
        previous['sourceBindings']['/observations/1'] = 'v6:another'
        expected = {}
    elif case == 'ambiguous_current':
        current['observations'].append({**item, 'flag': '偏高'})
        expected = {}
    elif case == 'different_date':
        current['reportTime'] = '2026-02-01'
        expected = {}
    before = deepcopy((current, previous))
    result = _inherit_published_bindings(current, previous)
    assert result.get('sourceBindings', {}) == expected
    assert result['observations'] == current['observations']
    assert (current, previous) == before
