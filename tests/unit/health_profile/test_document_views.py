"""Read-only document display, fixed paging, lifecycle and fact governance."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.contracts import ProfilePublicationWatermarkV1
from health_profile.contracts import ProfileScope
from health_profile.document_archive_contracts import ProfileArchivedExtraction
from health_profile.document_archive_contracts import ProfileDocumentArchiveChangedError
from health_profile.document_archive_contracts import ProfileDocumentArchiveDescriptor
from health_profile.document_archive_contracts import ProfileDocumentArchivePage
from health_profile.document_archive_contracts import ProfileDocumentArchiveReadError
from health_profile.document_archive_contracts import ProfileDocumentSourceState
from health_profile.document_views import ProfileDocumentObservationViews
from health_profile.models import FactStatus
from health_profile.models import VerificationStatus
from health_profile.report_ingestion import _report_observations
from health_profile.service import HealthProfileConflictError
from health_profile.service import HealthProfileService
from health_profile.service import HealthProfileValidationError
from modules.analysis.application.extraction import merge_document_results
from tests.unit.health_profile.test_public_contracts import setup_flow


class Archives:
    def __init__(self, scope=('u', 's')):
        self.scope = scope
        self.values = {}
        self.states = {}
        self.error = None
        self.reads = 0

    def add(self, document_id, result=None, *, source_observations=(), **options):
        descriptor = ProfileDocumentArchiveDescriptor(
            *self.scope,
            extraction_id='extraction-' + document_id,
            document_id=document_id,
            canonical_report_id='source-' + document_id,
            lifecycle_token='lifecycle-1',
            **options,
        )
        extraction = ProfileArchivedExtraction(descriptor, deepcopy(result), source_observations)
        self.values[descriptor.extraction_id] = extraction
        return extraction

    async def list_archived(self, *, user_id, subject_id, document_id=None, after_id=None, limit=20):
        if self.error:
            raise self.error
        selected = [
            value.descriptor
            for value in self.values.values()
            if (value.descriptor.user_id, value.descriptor.subject_id) == (user_id, subject_id)
            and (document_id is None or document_id in (value.descriptor.document_id, *value.descriptor.document_ids))
            and (after_id is None or value.descriptor.document_id > after_id)
        ]
        selected.sort(key=lambda value: value.document_id)
        return ProfileDocumentArchivePage(
            tuple(selected[:limit]),
            selected[limit - 1].document_id if len(selected) > limit else None,
        )

    async def read_fixed(self, *, user_id, subject_id, extraction_id, expected_lifecycle_token=None, document_id=None):
        if self.error:
            raise self.error
        self.reads += 1
        value = self.values[extraction_id]
        if (value.descriptor.user_id, value.descriptor.subject_id) != (user_id, subject_id):
            raise ProfileDocumentArchiveReadError('foreign scope')
        if expected_lifecycle_token and value.descriptor.lifecycle_token != expected_lifecycle_token:
            raise ProfileDocumentArchiveChangedError('source changed')
        if document_id is not None:
            if document_id not in (value.descriptor.document_id, *value.descriptor.document_ids):
                raise ProfileDocumentArchiveReadError('foreign document filter')
            value = replace(
                value,
                result=value.result if document_id == value.descriptor.document_id else None,
                source_observations=tuple(
                    row
                    for row in value.source_observations
                    if 'documentIds' not in row or document_id in row['documentIds']
                ),
            )
        return value

    async def read_source_state(self, *, user_id, subject_id, source_id):
        return self.states.get(source_id, ProfileDocumentSourceState('legacy-1', True, 'active'))


def reader(archives, *, legacy=None, facts=()):
    repository = SimpleNamespace(list_fact_page=AsyncMock(return_value=facts))
    return ProfileDocumentObservationViews(archives=archives, repository=repository, legacy=legacy)


def neutral(pointer='/observations/0', *, value=0, kind='base', revision=3):
    return {
        'observationRef': pointer,
        'sourcePointer': '/contributions/c' + pointer,
        'contributionKind': kind,
        'sourceRevision': revision,
        'label': 'test',
        'rawValue': value,
        'observedAt': '2026-11-01',
    }


@pytest.mark.asyncio
async def test_complete_retains_undated_zero_unresolved_narratives_and_metadata_without_facts():
    archives = Archives()
    archives.add(
        '1',
        {
            'reportType': 'checkup',
            'aiSummary': 'not a source observation',
            'observations': [{'itemName': 'zero', 'resultValue': 0}],
            'unresolvedItems': [
                {
                    'itemName': 'missing',
                    'resultValue': None,
                    'missingFields': ['resultValue'],
                    'reason': 'missing_result',
                }
            ],
            'sections': [
                {
                    'reportType': 'imaging',
                    'reportTime': '2026-11-01',
                    'expansion': {'findings': 'finding', 'diagnosis': 'source diagnosis'},
                }
            ],
        },
    )
    archives.add('2', {'reportType': 'unknown', 'title': 'metadata only'}, title='metadata only')
    views = reader(archives)
    page = await views.list_observations('u', 's', view='complete', limit=10)
    assert len(page['items']) == 4
    assert len(page['documents']) == 2
    zero = next(item for item in page['items'] if item['observation']['itemName'] == 'zero')
    assert zero['observation']['resultValue'] == 0
    assert zero['observedAt'] is None and zero['dateStatus'] == 'unknown'
    assert zero['verificationStatus'] == 'unconfirmed' and zero['factStatus'] is None
    unresolved = next(item for item in page['items'] if item['resolutionStatus'] == 'unresolved')
    assert unresolved['missingFields'] == ['resultValue']
    assert unresolved['factEligibility'] == 'unresolved'
    assert page['nextCursor'] is None and page['status'] == 'ready'
    views.repository.list_fact_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_standard_reads_formal_observations_without_unpublished_or_unresolved_content():
    archives = Archives()
    archives.add(
        '1',
        {'unresolvedItems': [{'itemName': 'missing'}]},
        source_status='active',
        source_revision=3,
        source_observations=(neutral(),),
    )
    archives.add('2', {'observations': [{'itemName': 'pending', 'resultValue': 1}]})
    page = await reader(archives).list_observations('u', 's')
    assert len(page['items']) == 1
    assert page['items'][0]['sourceRevision'] == 3
    assert page['items'][0]['observation']['resultValue'] == 0
    assert page['items'][0]['dateStatus'] == 'known'
    assert 'documents' not in page and 'resolutionStatus' not in page['items'][0]


@pytest.mark.asyncio
async def test_item_cursor_pins_one_file_and_rejects_changed_lifecycle():
    archives = Archives()
    value = archives.add(
        '1', {'observations': [{'itemName': 'a', 'resultValue': 1}, {'itemName': 'b', 'resultValue': 2}]}
    )
    views = reader(archives)
    first = await views.list_observations('u', 's', view='complete', limit=1)
    second = await views.list_observations('u', 's', view='complete', limit=1, cursor=first['nextCursor'])
    assert [item['observation']['itemName'] for item in first['items'] + second['items']] == ['a', 'b']
    archives.values[value.descriptor.extraction_id] = replace(
        value,
        descriptor=replace(value.descriptor, lifecycle_token='withdrawn-2', source_status='withdrawn'),
    )
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await views.list_observations('u', 's', view='complete', cursor=first['nextCursor'])


@pytest.mark.asyncio
@pytest.mark.parametrize('archive_status', ['pending', 'unavailable'])
async def test_item_cursor_survives_temporary_archive_failure_without_skipping_unread_items(archive_status):
    archives = Archives()
    archived = archives.add(
        '1', {'observations': [{'itemName': 'a', 'resultValue': 1}, {'itemName': 'b', 'resultValue': 2}]}
    )
    views = reader(archives)
    first = await views.list_observations('u', 's', view='complete', limit=1)
    assert [item['observation']['itemName'] for item in first['items']] == ['a']
    archives.values[archived.descriptor.extraction_id] = replace(
        archived,
        descriptor=replace(archived.descriptor, archive_status=archive_status, reason='temporary_archive_failure'),
        result=None,
    )
    before = deepcopy(archives.values)
    unavailable = await views.list_observations('u', 's', view='complete', limit=1, cursor=first['nextCursor'])
    assert unavailable['items'] == [] and unavailable['status'] == 'unavailable'
    assert unavailable['nextCursor'] == first['nextCursor']
    assert unavailable['documents'][0]['archiveStatus'] == archive_status
    assert archives.values == before
    archives.values[archived.descriptor.extraction_id] = archived
    before = deepcopy(archives.values)
    resumed = await views.list_observations('u', 's', view='complete', limit=1, cursor=unavailable['nextCursor'])
    assert [item['observation']['itemName'] for item in resumed['items']] == ['b']
    assert resumed['nextCursor'] is None
    assert archives.values == before
    views.repository.list_fact_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'subject_id': 'other'}, {'document_id': 'other'}, {'view': 'standard'}])
async def test_cursor_cannot_cross_scope_filter_or_view(change):
    archives = Archives()
    archives.add('1', {'observations': [{'itemName': 'a', 'resultValue': 1}, {'itemName': 'b', 'resultValue': 2}]})
    views = reader(archives)
    page = await views.list_observations('u', 's', view='complete', limit=1)
    options = {'subject_id': 's', 'view': 'complete', 'cursor': page['nextCursor'], **change}
    with pytest.raises(ValueError):
        await views.list_observations('u', **options)


@pytest.mark.asyncio
async def test_no_item_documents_consume_one_page_unit_and_withdrawal_exposes_no_values():
    archives = Archives()
    archives.add(
        '1',
        {'observations': [{'itemName': 'secret', 'resultValue': 100}]},
        source_status='withdrawn',
        archive_status='withdrawn',
    )
    archives.add('2', None, archive_status='unavailable', reason='historical_archive_missing')
    first = await reader(archives).list_observations('u', 's', view='complete', limit=1)
    assert first['items'] == [] and len(first['documents']) == 1 and first['nextCursor']
    assert first['documents'][0]['sourceStatus'] == 'withdrawn'
    assert 'secret' not in str(first)
    second = await reader(archives).list_observations('u', 's', view='complete', limit=1, cursor=first['nextCursor'])
    assert second['items'] == [] and second['status'] == 'unavailable'
    assert second['documents'][0]['reason'] == 'historical_archive_missing'


@pytest.mark.asyncio
async def test_legacy_rows_do_not_bypass_managed_or_withdrawn_lifecycle():
    archives = Archives()
    archives.states['old'] = ProfileDocumentSourceState('withdrawn', False, 'withdrawn')
    legacy = SimpleNamespace(
        list_observations=AsyncMock(
            return_value={
                'items': [{'canonicalReportId': 'old', 'observation': {'resultValue': 'secret'}}],
                'nextCursor': None,
            }
        )
    )
    page = await reader(archives, legacy=legacy).list_observations('u', 's', view='complete')
    assert page['items'] == [] and 'secret' not in str(page)


@pytest.mark.asyncio
async def test_legacy_lifecycle_race_raises_instead_of_returning_old_value():
    archives = Archives()
    archives.read_source_state = AsyncMock(
        side_effect=[
            ProfileDocumentSourceState('legacy-1', True, 'active'),
            ProfileDocumentSourceState('withdrawn', False, 'withdrawn'),
        ]
    )
    legacy = SimpleNamespace(
        list_observations=AsyncMock(
            return_value={
                'items': [{'canonicalReportId': 'old', 'observation': {'resultValue': 'secret'}}],
                'nextCursor': None,
            }
        )
    )
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await reader(archives, legacy=legacy).list_observations('u', 's')


@pytest.mark.asyncio
async def test_fact_status_requires_unique_exact_source_revision_and_pointer():
    archives = Archives()
    observation = {'itemName': 'test', 'resultValue': 0}
    archives.add(
        '1',
        {'observations': [observation], 'expansion': [observation]},
        source_status='active',
        source_revision=3,
        source_observations=(neutral(),),
    )
    fact = SimpleNamespace(
        fact_id='fact',
        verification_status=VerificationStatus.CONFIRMED,
        status=FactStatus.ACTIVE,
        sources=(
            SimpleNamespace(
                source_type='health_report',
                source_id='source-1',
                source_revision=3,
                pointer='/contributions/c/observations/0',
            ),
        ),
    )
    page = await reader(archives, facts=(fact,)).list_observations('u', 's', view='complete')
    assert page['items'][0]['verificationStatus'] == 'confirmed'
    assert page['items'][0]['factStatus'] == 'active'
    wrong = deepcopy(fact)
    wrong.sources[0].source_revision = 4
    page = await reader(archives, facts=(wrong,)).list_observations('u', 's', view='complete')
    assert page['items'][0]['factStatus'] is None
    duplicate = deepcopy(fact)
    duplicate.fact_id = 'another'
    page = await reader(archives, facts=(fact, duplicate)).list_observations('u', 's', view='complete')
    assert page['items'][0]['verificationStatus'] == 'unconfirmed'


@pytest.mark.asyncio
async def test_distinct_physical_items_survive_but_bound_section_and_expansion_mirrors_do_not():
    archives = Archives()
    item = {'itemName': 'same', 'resultValue': 1, 'observedAt': '2026-11-01'}
    archives.add(
        '1',
        {
            'observations': [item, {**item, 'observedAt': '2026-11-02'}],
            'expansion': [item],
            'sections': [{'reportType': 'lab', 'observations': [item]}],
            'sourceBindings': {
                '/sections/0/observations/0': 'v6:physical-1',
                '/observations/0': 'v6:physical-1',
                '/expansion/0': 'v6:physical-1',
            },
        },
    )
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert len(page['items']) == 2
    assert {item['observedAt'][:10] for item in page['items']} == {'2026-11-01', '2026-11-02'}


@pytest.mark.asyncio
async def test_real_merged_report_does_not_duplicate_public_projections_without_bindings():
    parts = [
        {
            'reportType': 'lab',
            'reportTime': date,
            'status': 'normal',
            'observations': [{'itemName': 'same', 'resultValue': 1, 'specimen': specimen}],
            'unresolvedItems': [{'itemName': 'pending-' + specimen, 'missingFields': ['resultValue']}],
            'expansion': [],
        }
        for date, specimen in (('2026-11-01', 'serum'), ('2026-11-02', 'plasma'))
    ]
    result = merge_document_results(parts)
    assert 'sourceBindings' not in result
    archives = Archives()
    archives.add('1', result)
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert len(page['items']) == 4
    resolved = [item for item in page['items'] if item['resolutionStatus'] == 'resolved']
    assert {item['observation']['specimen'] for item in resolved} == {'serum', 'plasma'}
    assert all(item['sourcePointer'].startswith('/sections/') for item in page['items'])


@pytest.mark.asyncio
async def test_standard_source_identity_does_not_change_when_archive_or_lifecycle_changes():
    archives = Archives()
    first = archives.add('1', {}, source_status='active', source_revision=3, source_observations=(neutral(),))
    views = reader(archives)
    before = await views.list_observations('u', 's')
    archives.values[first.descriptor.extraction_id] = replace(
        first,
        descriptor=replace(first.descriptor, extraction_id='new-extraction', lifecycle_token='new-lifecycle'),
    )
    changed = archives.values.pop(first.descriptor.extraction_id)
    archives.values[changed.descriptor.extraction_id] = changed
    after = await views.list_observations('u', 's')
    assert before['items'][0]['observationId'] == after['items'][0]['observationId']


@pytest.mark.asyncio
async def test_service_preserves_initialization_and_maps_conflict_without_swallowing_it():
    flow = await setup_flow()
    archives = Archives((flow.scope.user_id, flow.scope.subject_id))
    archives.add('1', {'unresolvedItems': [{'itemName': 'test', 'missingFields': ['resultValue']}]})
    service = HealthProfileService(flow.repository, document_archive_reader=archives)
    result = await service.get_profile('u', document_observation_view='complete')
    assert result['documentObservations']['items'][0]['resolutionStatus'] == 'unresolved'
    archives.error = ProfileDocumentArchiveChangedError('changed')
    with pytest.raises(HealthProfileConflictError):
        await service.get_profile('u', document_observation_view='complete')
    archives.error = ProfileDocumentArchiveReadError('unavailable')
    result = await service.get_profile('u', document_observation_view='complete')
    assert result['documentObservations']['status'] == 'unavailable'
    with pytest.raises(HealthProfileValidationError):
        await service.get_profile('u', document_observation_view='invalid')
    with pytest.raises(HealthProfileValidationError):
        await service.list_document_observations('u', cursor='invalid', view='complete')


@pytest.mark.asyncio
async def test_archive_reader_binding_is_once_during_assembly_and_same_instance_is_idempotent():
    flow = await setup_flow()
    archives = Archives((flow.scope.user_id, flow.scope.subject_id))
    service = HealthProfileService(flow.repository)
    service.bind_document_archive_reader(archives)
    service.bind_document_archive_reader(archives)
    with pytest.raises(RuntimeError):
        service.bind_document_archive_reader(Archives())
    await service.list_document_observations('u', view='complete')
    service.bind_document_archive_reader(archives)
    unbound = HealthProfileService(flow.repository)
    await unbound.list_document_observations('u')
    with pytest.raises(RuntimeError):
        unbound.bind_document_archive_reader(archives)


@pytest.mark.asyncio
async def test_readable_material_survives_other_document_missing_archive():
    archives = Archives()
    archives.add('1', None, archive_status='unavailable', reason='missing_archive')
    archives.add('2', {'observations': [{'itemName': 'readable', 'resultValue': 'negative'}]})
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert page['status'] == 'partial'
    assert len(page['documents']) == 2
    assert page['items'][0]['observation']['resultValue'] == 'negative'


@pytest.mark.asyncio
async def test_report_source_contribution_is_preserved_separately_from_base_result():
    archives = Archives()
    archives.add(
        '1',
        {'observations': [{'itemName': 'base', 'resultValue': 'normal'}]},
        source_status='active',
        source_revision=5,
        source_observations=(neutral('/reportSource/metrics/0', kind='legacy_report_source', revision=5),),
    )
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert len(page['items']) == 2
    assert {item['observation']['itemName'] for item in page['items']} == {'base', 'test'}


@pytest.mark.asyncio
async def test_fact_lookup_budget_keeps_unproven_associations_unconfirmed():
    archives = Archives()
    archives.add(
        '1',
        {'observations': [{'itemName': 'test', 'resultValue': 0}]},
        source_status='active',
        source_revision=3,
        source_observations=(neutral(),),
    )
    views = reader(archives, facts=(SimpleNamespace(), SimpleNamespace()))
    views.SCAN_BUDGET = 1
    page = await views.list_observations('u', 's', view='complete')
    assert page['items'][0]['verificationStatus'] == 'unconfirmed'
    assert views.repository.list_fact_page.await_count == 1
    assert views.repository.list_fact_page.await_args.kwargs['limit'] == 2


@pytest.mark.asyncio
async def test_lifecycle_change_after_material_read_but_before_response_is_rejected():
    archives = Archives()
    extraction = archives.add('1', {'observations': [{'itemName': 'test', 'resultValue': 'secret'}]})
    original_read = archives.read_fixed

    async def read(**options):
        value = await original_read(**options)
        if archives.reads == 1:
            archives.values[extraction.descriptor.extraction_id] = replace(
                extraction,
                descriptor=replace(extraction.descriptor, lifecycle_token='withdrawn'),
            )
        return value

    archives.read_fixed = read
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await reader(archives).list_observations('u', 's', view='complete')


@pytest.mark.asyncio
async def test_new_pending_parse_is_complete_while_standard_keeps_old_active_source():
    archives = Archives()
    old_base = {**neutral(value=1), 'sourceStatus': 'active', 'extractionId': 'old-extraction'}
    old_report = {
        **neutral('/reportSource/metrics/0', value=5, kind='legacy_report_source'),
        'sourceStatus': 'active',
        'extractionId': 'old-extraction',
    }
    pending_row = {**neutral('/observations/1', value=9), 'sourceStatus': 'pending'}
    archives.add(
        '1',
        {'observations': [{'itemName': 'test', 'resultValue': 2, 'observedAt': '2026-11-01'}]},
        source_status='pending',
        source_revision=None,
        publication_revision=None,
        source_observations=(old_base, old_report, pending_row),
    )
    fact = SimpleNamespace(
        fact_id='old-fact',
        verification_status=VerificationStatus.CONFIRMED,
        status=FactStatus.ACTIVE,
        sources=(
            SimpleNamespace(
                source_type='health_report',
                source_id='source-1',
                source_revision=3,
                pointer='/contributions/c/observations/0',
            ),
        ),
    )
    views = reader(archives, facts=(fact,))
    standard = await views.list_observations('u', 's')
    assert [item['observation']['resultValue'] for item in standard['items']] == [1, 5]
    assert all(item['sourceRevision'] == 3 for item in standard['items'])
    assert all(item['factEligibility'] == 'subject_to_fact_policy' for item in standard['items'])
    complete = await views.list_observations('u', 's', view='complete')
    assert len(complete['items']) == 1
    item = complete['items'][0]
    assert item['observation']['resultValue'] == 2
    assert item['extractionId'] == 'extraction-1'
    assert item['sourceStatus'] == 'pending' and item['sourceRevision'] is None
    assert item['publicationRevision'] is None and item['factEligibility'] == 'source_not_published'
    assert item['verificationStatus'] == 'unconfirmed' and item['factStatus'] is None
    views.repository.list_fact_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('archive_status', ['pending', 'unavailable'])
async def test_unreadable_new_archive_keeps_only_proven_active_rows_in_standard(archive_status):
    archives = Archives()
    archives.add(
        '1',
        None,
        archive_status=archive_status,
        source_status='unavailable',
        source_revision=None,
        source_observations=(
            {**neutral(value=1), 'sourceStatus': 'active', 'extractionId': 'old-extraction'},
            {**neutral('/observations/1', value=2), 'sourceStatus': 'pending'},
            neutral('/observations/2', value=3),
        ),
    )
    views = reader(archives)
    standard = await views.list_observations('u', 's')
    assert [item['observation']['resultValue'] for item in standard['items']] == [1]
    assert standard['items'][0]['sourceRevision'] == 3
    assert standard['items'][0]['factEligibility'] == 'subject_to_fact_policy'
    complete = await views.list_observations('u', 's', view='complete')
    assert complete['items'] == [] and complete['status'] == 'unavailable'
    assert complete['documents'][0]['extractionId'] == 'extraction-1'
    assert complete['documents'][0]['archiveStatus'] == archive_status


@pytest.mark.asyncio
@pytest.mark.parametrize('withdrawn_field', ['source_status', 'archive_status'])
async def test_withdrawal_hides_even_explicitly_active_source_rows(withdrawn_field):
    archives = Archives()
    archives.add(
        '1',
        None,
        source_observations=({**neutral(), 'sourceStatus': 'active'},),
        **{withdrawn_field: 'withdrawn'},
    )
    assert (await reader(archives).list_observations('u', 's'))['items'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [[], ['/expansion/0']])
async def test_complete_ignores_historical_publication_allowlist_without_losing_real_duplicates(allowed):
    archives = Archives()
    repeated = {'itemName': 'same printed measurement', 'resultValue': 0}
    other = {'itemName': 'other measurement', 'resultValue': 2}
    extra = {'itemName': 'expansion only', 'resultValue': 3}
    result = {
        'reportType': 'lab',
        'observations': [repeated, deepcopy(repeated), other],
        'expansion': [deepcopy(repeated), deepcopy(repeated), deepcopy(other), extra],
        'verifiedObservationPointers': allowed,
    }
    original = deepcopy(result)
    archives.add('1', result)
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert [item['sourcePointer'] for item in page['items']] == [
        '/observations/0',
        '/observations/1',
        '/observations/2',
        '/expansion/3',
    ]
    assert [item['observation']['resultValue'] for item in page['items']] == [0, 0, 2, 3]
    assert len({item['observationId'] for item in page['items']}) == 4
    assert result == original
    assert len(_report_observations(result)) == len(allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['observations', 'expansion'])
async def test_complete_reads_single_historical_array_despite_empty_publication_allowlist(field):
    archives = Archives()
    archives.add(
        '1',
        {
            'reportType': 'lab',
            field: [{'itemName': 'saved measurement', 'resultValue': 1}],
            'verifiedObservationPointers': [],
        },
    )
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert len(page['items']) == 1
    assert page['items'][0]['sourcePointer'] == f'/{field}/0'


@pytest.mark.asyncio
async def test_complete_uses_item_file_proof_without_inheriting_all_source_attachments():
    archives = Archives()
    archived = archives.add(
        'primary',
        {
            'observations': [
                {'itemName': 'base measurement', 'resultValue': 1},
                {'itemName': 'joint measurement', 'resultValue': 2},
                {'itemName': 'new unmatched measurement', 'resultValue': 3},
            ],
            'unresolvedItems': [{'itemName': 'pending measurement', 'missingFields': ['resultValue']}],
        },
        document_ids=('primary', 'secondary'),
        source_status='active',
        source_revision=3,
        source_observations=(
            {**neutral(value=1), 'documentId': 'primary', 'documentIds': ['primary']},
            {
                **neutral('/observations/1', value=2),
                'documentId': None,
                'documentIds': ['primary', 'secondary'],
            },
            {
                **neutral('/reportSource/metrics/0', value=4, kind='legacy_report_source'),
                'documentId': 'secondary',
                'documentIds': ['secondary'],
            },
        ),
    )
    page = await reader(archives).list_observations('u', 's', view='complete')
    assert [(item['documentId'], item['documentIds']) for item in page['items']] == [
        ('primary', ['primary']),
        (None, ['primary', 'secondary']),
        ('primary', ['primary']),
        ('primary', ['primary']),
        ('secondary', ['secondary']),
    ]
    assert page['documents'][0]['documentIds'] == ['primary', 'secondary']
    archives.values[archived.descriptor.extraction_id] = replace(
        archived,
        descriptor=replace(archived.descriptor, source_status='pending', source_revision=None),
        source_observations=tuple(
            {**original, 'extractionId': 'previous-interpretation'} for original in archived.source_observations
        ),
    )
    pending = await reader(archives).list_observations('u', 's', view='complete')
    assert len(pending['items']) == 4
    assert all(item['documentId'] == 'primary' and item['documentIds'] == ['primary'] for item in pending['items'])
    assert all(item['factStatus'] is None for item in pending['items'])


@pytest.mark.asyncio
async def test_complete_fact_association_accepts_only_matching_explicit_parse_identity():
    archives = Archives()
    current = {**neutral(value=2), 'sourceStatus': 'active', 'extractionId': 'extraction-1'}
    archives.add(
        '1',
        {'observations': [{'itemName': 'test', 'resultValue': 2}]},
        source_status='active',
        source_revision=3,
        source_observations=(current,),
    )
    fact = SimpleNamespace(
        fact_id='current-fact',
        verification_status=VerificationStatus.VERIFIED_SOURCE,
        status=FactStatus.ACTIVE,
        sources=(
            SimpleNamespace(
                source_type='health_report',
                source_id='source-1',
                source_revision=3,
                pointer='/contributions/c/observations/0',
            ),
        ),
    )
    page = await reader(archives, facts=(fact,)).list_observations('u', 's', view='complete')
    assert page['items'][0]['verificationStatus'] == 'verified_source'
    assert page['items'][0]['factStatus'] == 'active'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'job_status,source_status',
    [
        ('pending', 'pending'),
        ('dead_letter', 'unavailable'),
        ('completed', 'active'),
    ],
)
async def test_legacy_complete_uses_real_job_state_and_standard_preserves_original_row(job_status, source_status):
    archives = Archives()
    archives.states['legacy'] = ProfileDocumentSourceState('legacy-token', True, 'not_published')
    original = {
        'observationId': 'legacy:original',
        'documentId': 'legacy-document',
        'documentIds': ['legacy-document'],
        'canonicalReportId': 'legacy',
        'sourceRevision': 17,
        'sourcePointer': '/expansion/0',
        'observation': {'itemName': 'legacy item', 'resultValue': 0},
        'observedAt': None,
        'dateStatus': 'unknown',
        'ingestionStatus': job_status,
        'factEligibility': 'date_unknown',
    }
    legacy = SimpleNamespace(
        list_observations=AsyncMock(
            return_value={
                'items': [original],
                'nextCursor': None,
            }
        )
    )
    views = reader(archives, legacy=legacy)
    complete = await views.list_observations('u', 's', view='complete')
    assert complete['items'][0]['sourceStatus'] == source_status
    assert complete['items'][0]['publicationRevision'] is None
    assert complete['documents'][0]['sourceStatus'] == source_status
    assert complete['documents'][0]['archiveStatus'] == 'unavailable'
    standard = await views.list_observations('u', 's')
    assert standard['items'] == [original]
    assert 'sourceStatus' not in original and 'publicationRevision' not in original


@pytest.mark.asyncio
@pytest.mark.parametrize('view', ['standard', 'complete'])
async def test_secondary_document_filter_keeps_canonical_combined_source_across_item_pages(view):
    archives = Archives()
    primary = archives.add(
        'a',
        {'observations': [{'itemName': 'A original', 'resultValue': 1}]},
        document_ids=('a', 'b'),
        source_status='active',
        source_revision=3,
        source_observations=(
            {**neutral(value=1), 'documentId': 'a', 'documentIds': ['a']},
            {
                **neutral('/reportSource/metrics/0', value=2, kind='legacy_report_source'),
                'documentId': None,
                'documentIds': ['a', 'b'],
            },
        ),
    )
    secondary = archives.add(
        'b',
        {'observations': [{'itemName': 'B original', 'resultValue': 3}]},
        source_status='active',
        source_revision=3,
        source_observations=({**neutral(value=3), 'documentId': 'b', 'documentIds': ['b']},),
    )
    provider_cursor = 'opaque:related-source:owned-b'
    archives.list_archived = AsyncMock(
        side_effect=[
            ProfileDocumentArchivePage((secondary.descriptor,), provider_cursor),
            ProfileDocumentArchivePage((primary.descriptor,), None),
        ]
    )
    views = reader(archives)
    cursor, items = None, []
    for _ in range(5):
        page = await views.list_observations('u', 's', document_id='b', view=view, limit=1, cursor=cursor)
        items.extend(page['items'])
        cursor = page['nextCursor']
        if cursor is None:
            break
    assert cursor is None
    assert [item['observation']['resultValue'] for item in items] == [3, 2]
    assert [item['canonicalReportId'] for item in items] == ['source-b', 'source-a']
    assert all('b' in item['documentIds'] for item in items)
    assert archives.list_archived.await_args_list[1].kwargs['after_id'] == provider_cursor
    if view == 'standard':
        assert items[0]['documentId'] == 'b' and items[1]['documentId'] is None


def unproved_archive(archives, document_id):
    value = archives.add(document_id, None, archive_status='unavailable', reason='fixed_archive_missing')
    archives.values[value.descriptor.extraction_id] = replace(
        value,
        descriptor=replace(value.descriptor, extraction_id=None),
    )
    archives.states[value.descriptor.canonical_report_id] = ProfileDocumentSourceState(
        value.descriptor.lifecycle_token,
        True,
        'not_published',
    )
    return archives.values[value.descriptor.extraction_id].descriptor


@pytest.mark.asyncio
async def test_unproved_archives_page_as_distinct_documents_without_fixed_dereferences():
    archives = Archives()
    unproved_archive(archives, '1')
    unproved_archive(archives, '2')
    archives.read_fixed = AsyncMock(side_effect=AssertionError('no fixed archive exists'))
    views = reader(archives)
    first = await views.list_observations('u', 's', view='complete', limit=1)
    assert first['items'] == [] and len(first['documents']) == 1 and first['nextCursor']
    assert first['documents'][0]['documentId'] == '1'
    assert first['documents'][0]['extractionId'] is None
    second = await views.list_observations('u', 's', view='complete', limit=1, cursor=first['nextCursor'])
    assert second['items'] == [] and len(second['documents']) == 1 and second['nextCursor'] is None
    assert second['documents'][0]['documentId'] == '2'
    both = await views.list_observations('u', 's', view='complete', limit=2)
    assert [document['documentId'] for document in both['documents']] == ['1', '2']
    assert both['status'] == 'unavailable'
    standard = await views.list_observations('u', 's', limit=1)
    assert standard == {'items': [], 'nextCursor': None}
    archives.read_fixed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unproved_archive_metadata_rechecks_source_lifecycle_without_fixed_read():
    archives = Archives()
    unproved_archive(archives, '1')
    archives.read_fixed = AsyncMock(side_effect=AssertionError('no fixed archive exists'))
    archives.read_source_state = AsyncMock(return_value=ProfileDocumentSourceState('withdrawn', False, 'withdrawn'))
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await reader(archives).list_observations('u', 's', view='complete')
    archives.read_fixed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unproved_unavailable_archive_keeps_proven_legacy_observations():
    archives = Archives()
    unproved_archive(archives, '1')
    archives.read_fixed = AsyncMock(side_effect=AssertionError('no fixed archive exists'))
    original = {
        'observationId': 'old-observation',
        'documentId': '1',
        'documentIds': ['1'],
        'canonicalReportId': 'source-1',
        'sourceRevision': 9,
        'sourcePointer': '/observations/0',
        'observation': {'itemName': 'old original', 'resultValue': 0},
        'ingestionStatus': 'completed',
        'observedAt': None,
        'dateStatus': 'unknown',
    }
    legacy = SimpleNamespace(list_observations=AsyncMock(return_value={'items': [original], 'nextCursor': None}))
    page = await reader(archives, legacy=legacy).list_observations('u', 's', view='complete')
    assert page['items'][0]['observation']['resultValue'] == 0
    assert page['items'][0]['sourceStatus'] == 'active'
    assert len(page['documents']) == 1 and page['documents'][0]['extractionId'] is None
    archives.read_fixed.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_archive_requires_exact_profile_ownership_for_rows_and_metadata():
    archives = Archives()
    visible = {'itemName': 'owned', 'resultValue': 3, 'observedAt': '2026-11-01'}
    hidden = {'itemName': 'other subject', 'resultValue': 999, 'observedAt': '2026-11-01'}
    archives.add(
        '1',
        {'observations': [visible, hidden]},
        current_source=True,
        source_status='active',
        source_revision=3,
        source_observations=(neutral(value=3), neutral('/observations/1', value=999)),
    )
    archives.add(
        '2',
        {'observations': [hidden]},
        current_source=True,
        source_status='active',
        source_revision=3,
        source_observations=(neutral(value=999),),
        title='other subject document',
    )
    owned = SimpleNamespace(
        fact_id='fact',
        basic_info_lifecycle_epoch=1,
        verification_status=VerificationStatus.UNCONFIRMED,
        status=FactStatus.PENDING,
        sources=(
            SimpleNamespace(
                source_type='health_report',
                source_id='source-1',
                source_revision=3,
                pointer='/contributions/c/observations/0',
            ),
        ),
    )
    views = reader(archives, facts=(owned,))
    views.require_profile_ownership = True
    profile = SimpleNamespace(profile_version=1)
    views.repository.get_subject = AsyncMock(return_value=SimpleNamespace(status='active', lifecycle_epoch=1))
    views.repository.get_view = AsyncMock(return_value=profile)
    complete = await views.list_observations('u', 's', view='complete', limit=1)
    assert [row['observation']['resultValue'] for row in complete['items']] == [3]
    assert [document['documentId'] for document in complete['documents']] == ['1']
    assert complete['items'][0]['factStatus'] == 'pending'
    assert complete['nextCursor']
    profile.profile_version += 1
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await views.list_observations('u', 's', view='complete', cursor=complete['nextCursor'])
    standard = await views.list_observations('u', 's')
    assert [row['observation']['resultValue'] for row in standard['items']] == [3]
    assert standard['items'][0]['factStatus'] == 'pending'


@pytest.mark.asyncio
async def test_routed_subject_can_read_exact_original_evidence_from_upload_custody():
    archives = Archives(scope=('u', 'intake'))
    own = {'itemName': 'owned', 'resultValue': 7, 'observedAt': '2026-11-01'}
    wrong = {'itemName': 'unrelated', 'resultValue': 999, 'observedAt': '2026-11-01'}
    archives.add(
        '1',
        {'observations': [own, wrong]},
        current_source=True,
        source_status='active',
        source_revision=3,
        publication_revision=1,
        source_observations=(neutral(value=7), neutral('/observations/1', value=999)),
        title='intake document',
    )
    archives.add(
        '2',
        {'observations': [wrong]},
        current_source=True,
        source_status='active',
        source_revision=3,
        publication_revision=1,
        source_observations=(neutral(value=999),),
        title='unrelated document',
    )
    scope = ProfileScope(user_id='u', subject_id='intake')
    watermark = ProfilePublicationWatermarkV1(
        scope=scope,
        source_type='health_report',
        source_id='source-1',
        accepted_publication_revision=1,
        applied_publication_revision=1,
        settled_publication_revision=1,
        affected_subject_ids=('other',),
    )
    fact = SimpleNamespace(
        fact_id='fact',
        basic_info_lifecycle_epoch=1,
        verification_status=VerificationStatus.VERIFIED_SOURCE,
        status=FactStatus.ACTIVE,
        sources=(
            SimpleNamespace(
                source_type='health_report',
                source_id='source-1',
                source_revision=3,
                pointer='/contributions/c/observations/0',
            ),
        ),
    )
    views = reader(archives, facts=(fact,))
    views.require_profile_ownership = True
    views.publications = SimpleNamespace(list_watermarks=AsyncMock(return_value=(watermark,)))
    views.repository.get_subject = AsyncMock(return_value=SimpleNamespace(status='active', lifecycle_epoch=1))
    views.repository.get_view = AsyncMock(return_value=SimpleNamespace(profile_version=1))
    page = await views.list_observations('u', 'other', view='complete', document_id='1')
    assert [row['observation']['resultValue'] for row in page['items']] == [7]
    assert page['items'][0]['factStatus'] == 'active'
    assert [row['documentId'] for row in page['documents']] == ['1']
    assert page['documents'][0]['title'] is None
    assert archives.reads == 2
    assert not (await views.list_observations('u', 'other', document_id='2'))['items']
    views.publications.list_watermarks.return_value = (watermark.model_copy(update={'applied_retraction_revision': 1}),)
    assert not (await views.list_observations('u', 'other', view='complete'))['items']
