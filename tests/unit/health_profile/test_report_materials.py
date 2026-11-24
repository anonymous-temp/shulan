"""Public report material reads keep independent fixed identities and lifecycle."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health_profile.contracts import ProfileScope
from health_profile.document_archive_contracts import ProfileArchivedExtraction
from health_profile.document_archive_contracts import ProfileDocumentArchiveChangedError
from health_profile.document_archive_contracts import ProfileDocumentArchiveDescriptor
from health_profile.document_archive_contracts import ProfileDocumentArchivePage
from health_profile.document_archive_contracts import ProfileDocumentSourceState
from health_profile.report_materials import ProfileReportMaterialsCapacityError
from health_profile.report_materials import ProfileReportMaterialsReader

SCOPE = ProfileScope(user_id='test-user', subject_id='test-self')


def descriptor(**changes):
    return replace(
        ProfileDocumentArchiveDescriptor(
            'test-user',
            'test-self',
            'fixed-1',
            'document-1',
            'source-1',
            'lifecycle-1',
            interpretation_round=1,
            material_ref='material-1',
            result_ref='result-1',
            result_digest='a' * 64,
        ),
        **changes,
    )


def observation(value='0', date=None):
    return {'itemName': '合成检测', 'resultValue': value, 'observedAt': date}


def setup(entries, *, gender=()):
    profiles = SimpleNamespace(
        resolve_scope=AsyncMock(return_value=SCOPE),
        read_page=AsyncMock(
            return_value=SimpleNamespace(
                scope=SCOPE,
                status='ready',
                truncated=False,
                stale=False,
                facts=tuple(SimpleNamespace(status=status, value=value) for status, value in gender),
            )
        ),
    )
    by_id = {row.descriptor.extraction_id: row for row in entries}
    archives = SimpleNamespace(
        list_archived=AsyncMock(return_value=ProfileDocumentArchivePage(tuple(row.descriptor for row in entries))),
        read_fixed=AsyncMock(side_effect=lambda **kw: deepcopy(by_id[kw['extraction_id']])),
        read_source_state=AsyncMock(return_value=ProfileDocumentSourceState('lifecycle-1', False, 'active')),
    )
    return ProfileReportMaterialsReader(profiles=profiles, archives=archives), profiles, archives


@pytest.mark.asyncio
async def test_saved_undated_zero_unresolved_and_full_original_are_pure_material_without_fact_requirement():
    result = {'reportTime': None, 'observations': [observation()], 'unresolvedItems': [{'text': '合成待核实原文'}]}
    reader, profiles, archives = setup([ProfileArchivedExtraction(descriptor(), result)])
    payload = await reader.read(user_id='test-user')
    assert len(payload['materials']) == 2
    assert payload['materials'][0]['content']['resultValue'] == '0'
    assert payload['materials'][0]['metadata']['interpretation_round'] == 1
    assert payload['sourceSnapshot']['archives'][0]['result'] == result
    assert payload['coverage']['factVerification'] == 'saved_material_is_not_verified_patient_fact'
    assert profiles.resolve_scope.await_count == 2 and archives.read_fixed.await_count == 2


@pytest.mark.asyncio
async def test_missing_self_is_empty_without_archive_reads_and_explicit_other_subject_is_denied():
    reader, profiles, archives = setup([])
    profiles.resolve_scope.return_value = None
    assert (await reader.read(user_id='test-user'))['status'] == 'empty'
    archives.list_archived.assert_not_awaited()
    profiles.resolve_scope.return_value = SCOPE
    with pytest.raises(PermissionError):
        await reader.read(user_id='test-user', subject_id='family-member')
    archives.list_archived.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_latest_result_keeps_old_active_contribution_with_its_real_identity():
    old = {
        'label': '合成既有贡献',
        'rawValue': '0',
        'sourcePointer': '/contributions/old/observations/0',
        'observationRef': '/observations/0',
        'contributionKind': 'base',
        'contributionId': 'old',
        'extractionId': 'fixed-old',
        'sourceRevision': 3,
        'publicationRevision': 4,
        'sourceStatus': 'active',
        'contributionSnapshotRef': 'contribution-old',
    }
    current = descriptor(extraction_id='fixed-new', archive_status='unavailable', source_status='unavailable')
    reader, _, _ = setup([ProfileArchivedExtraction(current, None, (old,))])
    payload = await reader.read(user_id='test-user')
    assert payload['status'] == 'partial' and len(payload['materials']) == 1
    metadata = payload['materials'][0]['metadata']
    assert metadata['extractionId'] == 'fixed-old' and metadata['sourceRevision'] == 3
    assert metadata['contributionSnapshotRef'] == 'contribution-old'
    assert 'fixed-new' not in str(metadata)


@pytest.mark.asyncio
async def test_only_proven_original_mirrors_are_removed_and_distinct_dates_remain():
    original = {'observations': [observation('1', '2026-01-01'), observation('1', '2026-02-01')]}
    mirror = {
        'observationRef': '/observations/0',
        'sourcePointer': '/contributions/base/observations/0',
        'contributionKind': 'base',
        'extractionId': 'fixed-1',
        'sourceStatus': 'active',
        'rawValue': '1',
    }
    additional = {
        'observationRef': '/reportSource/0',
        'sourcePointer': '/contributions/supplement/reportSource/0',
        'contributionKind': 'legacy_report_source',
        'contributionSnapshotRef': 'separate',
        'rawValue': '1',
        'observedAt': '2026-03-01',
        'sourceStatus': 'active',
        'sourceRevision': 2,
    }
    reader, _, _ = setup([ProfileArchivedExtraction(descriptor(), original, (mirror, additional))])
    payload = await reader.read(user_id='test-user')
    assert len(payload['materials']) == 3 and payload['coverage']['mirrorsRemoved'] == 1
    assert len({row['id'] for row in payload['materials']}) == 3
    assert payload['materials'][0]['metadata']['mirrorSources'] == [mirror]


@pytest.mark.asyncio
async def test_empty_or_metadata_only_items_do_not_manufacture_personal_material():
    result = {'observations': [{}, {'itemName': '合成项目', 'unit': 'mg', 'referenceRange': '1-2'}]}
    reader, _, _ = setup([ProfileArchivedExtraction(descriptor(), result)])
    payload = await reader.read(user_id='test-user')
    assert payload['materials'] == [] and payload['status'] == 'empty'
    assert payload['coverage']['emptyObservationsExcluded'] == 2
    assert payload['sourceSnapshot']['archives'][0]['result'] == result


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['withdrawn', 'retired'])
async def test_withdrawn_or_retired_material_never_returns_saved_or_contribution_content(state):
    reader, _, _ = setup(
        [
            ProfileArchivedExtraction(
                descriptor(source_status=state),
                {'observations': [observation('synthetic-sensitive')]},
                ({'sourceStatus': 'active', 'rawValue': 'synthetic-sensitive'},),
            )
        ]
    )
    payload = await reader.read(user_id='test-user')
    assert payload['materials'] == []
    assert 'synthetic-sensitive' not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'facts,expected,status',
    [
        ([('active', 'male')], 'male', 'explicit'),
        ([('active', 'female')], 'female', 'explicit'),
        ([('active', 'male'), ('active', 'female')], None, 'conflicting'),
        ([('disputed', 'male')], None, 'conflicting'),
        ([('pending', 'female')], None, 'unknown'),
    ],
)
async def test_gender_only_uses_explicit_profile_values_and_is_not_clinical_material(facts, expected, status):
    reader, _, _ = setup([], gender=facts)
    payload = await reader.read(user_id='test-user')
    assert payload['gender'] == expected and payload['materials'] == []
    assert payload['coverage']['profileGenderStatus'] == status


@pytest.mark.asyncio
async def test_wrong_scope_program_error_and_cancellation_keep_their_original_meaning():
    reader, _, archives = setup([ProfileArchivedExtraction(descriptor(user_id='other-user'), {})])
    with pytest.raises(PermissionError):
        await reader.read(user_id='test-user')
    for error in (TypeError('synthetic bug'), PermissionError('synthetic permission'), asyncio.CancelledError()):
        archives.list_archived.side_effect = error
        with pytest.raises(type(error)) as captured:
            await reader.read(user_id='test-user')
        assert captured.value is error


@pytest.mark.asyncio
async def test_lifecycle_change_is_bounded_restart_and_never_returns_stale_content():
    reader, _, archives = setup([ProfileArchivedExtraction(descriptor(), {'observations': [observation()]})])
    archives.read_fixed.side_effect = ProfileDocumentArchiveChangedError('changed')
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await reader.read(user_id='test-user')
    assert archives.list_archived.await_count == 2


@pytest.mark.asyncio
async def test_document_capacity_fails_instead_of_claiming_complete_coverage():
    reader, _, archives = setup([ProfileArchivedExtraction(descriptor(), {'observations': [observation()]})])
    reader.max_documents = 1
    archives.list_archived.return_value = ProfileDocumentArchivePage((descriptor(),), 'more')
    with pytest.raises(ProfileReportMaterialsCapacityError):
        await reader.read(user_id='test-user')


@pytest.mark.asyncio
@pytest.mark.parametrize('withdraw', [None, 'report_only', 'independent_secondary'])
async def test_current_archive_uses_verified_clinical_rows_and_fixed_subject_lifecycle(monkeypatch, withdraw):
    from adapters.analysis.archived_extractions import create_document_archived_extraction_reader
    from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
    from adapters.profile_document_archives import ProfileDocumentArchiveAdapter
    from health_profile.document_archive_contracts import ProfileReportLifecycleConflict
    from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import cycle
    from tests.unit._support.current_document_profile import install_profile
    from tests.unit._support.current_document_profile import settle
    from tests.unit._support.current_occurrence import page
    from tests.unit._support.current_occurrence import readers_for
    from tests.unit._support.current_report_projection import command
    from tests.unit._support.current_report_projection import composition
    from tests.unit._support.current_report_projection import next_round
    from tests.unit._support.current_report_projection import projection
    from tests.unit._support.current_report_projection import report_contributions
    from tests.unit._support.current_report_projection import source

    flow = await composition(monkeypatch, [model_report(), model_report(), model_report()], png=True)
    flow.readers = readers_for(flow)
    install_profile(flow)
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )
    real_prepare = flow.prepared.prepare_bundle

    async def prepare_in_lifecycle(**kwargs):
        return await real_prepare(**kwargs, lifecycle_epoch=flow.epoch)

    monkeypatch.setattr(flow.prepared, 'prepare_bundle', prepare_in_lifecycle)
    primary, added = await page(flow, 'A'), await page(flow, 'B')
    report = await projection(flow, 'multi-page-health-analysis', primary, added)
    await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(primary),
        request_id='health-analysis-material-report',
        lifecycle_epoch=flow.epoch,
        report_projection_ref=report.snapshot_id,
    )
    await settle(flow)

    monkeypatch.setattr(
        flow.upstream.index_collection, 'with_options', lambda **_: flow.upstream.index_collection, raising=False
    )
    archive = create_document_archived_extraction_reader(
        processing=flow.analysis,
        lifecycle=DocumentPublicationLifecycleReader(
            ledger=flow.ledger, snapshots=flow.prepared.understanding.snapshots
        ),
        current_source=flow.readers.source,
    )
    provider = ProfileReportMaterialsReader(
        profiles=flow.profile.reader, archives=ProfileDocumentArchiveAdapter(archive)
    )
    keys = {'user_id': flow.scope.user_id, 'subject_id': flow.scope.subject_id, 'expected_lifecycle_epoch': flow.epoch}
    reader = flow.readers.report_projections.materials
    original_reader = reader.read_original
    reader.read_original = AsyncMock(side_effect=AssertionError('fixed archive read repeated original processing'))

    current = await provider.read(**keys)
    assert current['status'] == 'available'
    assert current['scope']['lifecycleEpoch'] == flow.epoch
    # The second page has a fixed archive but no independent publication.
    # Both verified ALT still appear once.
    assert all(material['metadata']['origin'] == 'source_contribution' for material in current['materials'])
    assert sorted(item['content']['label'] for item in current['materials']) == ['ALT', 'ALT', 'pH', 'pH', 'pH', 'pH']
    first_ref = current['materials'][0]['metadata']['sourceSnapshotRef']
    (combined,) = report_contributions(await source(flow, first_ref))
    if withdraw == 'report_only':
        await next_round(flow, primary)
        row = flow.upstream.index_collection.documents[primary.receipt.document_id]
        latest = row['archivedRounds'][max(row['archivedRounds'], key=int)]
        latest['interpretationDigest'] = 'a' * 64
        incomplete = await provider.read(**keys)
        assert incomplete['status'] == 'partial'
        assert sorted(item['content']['label'] for item in incomplete['materials']) == [
            'ALT',
            'ALT',
            'pH',
            'pH',
            'pH',
            'pH',
        ]
        # The withdrawing source no longer offers the latest original, and only
        # its fixed older publication has the matching secondary pointer.
        await flow.runtime.publisher.retract(
            scope=flow.scope,
            source_id=primary.material.source_id,
            source_revision=primary.material.source_revision,
            request_id='withdraw-multi-page-report',
            expected_prior_revision=1,
            lifecycle_epoch=flow.epoch,
        )
        assert not (await provider.read(**keys))['materials']
        await settle(flow)
        assert not (await provider.read(**keys))['materials']

        # Current withdrawals written before the predecessor ID was recorded
        # still resolve the archived intent by their exact source and revision.
        from adapters.analysis.publication import MongoDocumentPublicationLedger

        source_key = MongoDocumentPublicationLedger._key(flow.scope, primary.material.source_id)
        withdrawal = flow.ledger.sources.documents[source_key]['currentIntent']
        assert withdrawal.pop('withdrawalPredecessorId')
        assert not (await provider.read(**keys))['materials']
        return
    reader.read_original.assert_not_awaited()
    read_only_guard = reader.read_original
    reader.read_original = original_reader
    await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(added),
        request_id='health-analysis-material-record',
        lifecycle_epoch=flow.epoch,
    )
    await settle(flow)
    reader.read_original = read_only_guard
    current = await provider.read(**keys)
    observed = [material for material in current['materials'] if material['content']['label'] in {'ALT', 'pH'}]
    assert sorted(material['content']['label'] for material in observed) == ['ALT', 'ALT', 'pH', 'pH', 'pH', 'pH']
    assert all(material['metadata']['captureProofRef'].startswith('document_snapshot_') for material in observed)
    assert all(
        material['metadata']['sourceSnapshotRef'].startswith('document_source_snapshot_') for material in observed
    )
    assert all(material['metadata']['mirrorSources'] for material in observed if material['content']['label'] == 'ALT')
    reader.read_original.assert_not_awaited()
    if withdraw == 'independent_secondary':
        await flow.runtime.publisher.retract(
            scope=flow.scope,
            source_id=primary.material.source_id,
            source_revision=primary.material.source_revision,
            request_id='withdraw-multi-page-report',
            expected_prior_revision=1,
            lifecycle_epoch=flow.epoch,
        )
        for _ in range(2):
            (retained,) = (await provider.read(**keys))['materials']
            assert retained['metadata']['origin'] == 'source_contribution'
            assert retained['metadata']['documentId'] == added.receipt.document_id
            assert retained['content']['label'] == 'ALT'
            await settle(flow)
        return

    # Secondary provenance can be listed even if its independent index disappears.
    secondary = added.receipt.document_id
    secondary_index = flow.upstream.index_collection.documents.pop(secondary)
    page_result = await archive.list_archived(scope=flow.scope, document_id=secondary)
    (related,) = [item for item in page_result.entries if item.canonical_report_id == primary.material.source_id]
    assert {item.canonical_report_id for item in page_result.entries} == {
        primary.material.source_id,
        added.material.source_id,
    }
    linked = await archive.read_fixed(
        scope=flow.scope,
        extraction_id=related.extraction_id,
        expected_lifecycle_token=related.lifecycle_token,
        document_id=secondary,
    )
    assert linked.result is None
    assert sorted(row['label'] for row in linked.source_observations) == ['ALT', 'pH', 'pH']
    assert {row['documentId'] for row in linked.source_observations} == {secondary}
    with pytest.raises(PermissionError):
        await archive.read_fixed(
            scope=replace(flow.scope, user_id='foreign-tenant'),
            extraction_id=current['sourceSnapshot']['archives'][0]['descriptor']['extraction_id'],
        )

    flow.upstream.index_collection.documents[secondary] = secondary_index
    await flow.runtime.managed.execute(
        command(
            flow,
            primary,
            first_ref,
            1,
            'retire-multi-page-report',
            retire_contribution_ids=(combined['contributionId'],),
        )
    )
    await settle(flow)
    retired = await provider.read(**keys)
    assert len(retired['materials']) == 2
    assert all(item['content']['label'] == 'ALT' for item in retired['materials'])
    assert all(item['metadata']['origin'] == 'source_contribution' for item in retired['materials'])

    await cycle(flow)
    with pytest.raises(ProfileReportLifecycleConflict, match='lifecycle changed'):
        await provider.read(**keys)
    after = await provider.read(**{**keys, 'expected_lifecycle_epoch': flow.epoch + 2})
    assert not after['materials'] and not after['sourceSnapshot']['archives']


@pytest.mark.asyncio
async def test_current_undated_supplements_remain_distinct_and_disappear_after_withdrawal(monkeypatch):
    from modules.analysis.observation_lifecycle import DocumentCurrentObservationReader
    from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import actual_analysis_composition
    from tests.unit._support.current_document_profile import clinical_facts
    from tests.unit._support.current_document_profile import settle

    raw = model_report()
    raw['reportTime'] = None
    raw['observations'] = [
        {'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L'},
        {'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L', 'method': 'enzymatic'},
    ]
    flow = await actual_analysis_composition(
        monkeypatch,
        [raw],
        text='| Item | Result | Unit |\n| --- | --- | --- |\n| ALT | 12 | U/L |\n| ALT | 12 | U/L |',
    )
    prepared = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/undated.pdf', file_type='pdf'
    )
    await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(prepared),
        request_id='current-undated',
        lifecycle_epoch=flow.epoch,
    )

    async def epoch(scope):
        return await flow.profile.reader.resolve_self_lifecycle(
            ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id)
        )

    reader = DocumentCurrentObservationReader(
        lifecycle=DocumentPublicationLifecycleReader(
            ledger=flow.ledger, snapshots=flow.prepared.understanding.snapshots
        ),
        current_source=flow.readers.source,
        resolve_lifecycle=epoch,
    )
    assert not (await reader.read(scope=flow.scope)).items
    await settle(flow)
    facts = await clinical_facts(flow)
    assert len(facts) == 2
    assert len({fact.sources[0].pointer for fact in facts}) == 2
    assert all(
        fact.status == 'pending'
        and fact.verification_status == 'unconfirmed'
        and fact.effective_period['start'] is None
        for fact in facts
    )
    page = await reader.read(scope=flow.scope)
    assert [row['observation']['resultValue'] for row in page.items] == ['12', '12']
    assert all(row['dateStatus'] == 'unknown' for row in page.items)
    assert len({row['sourcePointer'] for row in page.items}) == 2
    assert (await reader.read(scope=flow.scope, limit=1)).truncated

    await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=prepared.material.source_id,
        source_revision=prepared.material.source_revision,
        request_id='retract-current-undated',
        expected_prior_revision=1,
        lifecycle_epoch=flow.epoch,
    )
    assert not (await reader.read(scope=flow.scope)).items
    await settle(flow)
    assert not await clinical_facts(flow)


@pytest.mark.asyncio
async def test_complete_current_archive_shows_new_fixed_round_while_standard_uses_applied_round(monkeypatch):
    from adapters.analysis.archived_extractions import create_document_archived_extraction_reader
    from adapters.profile_document_archives import ProfileDocumentArchiveAdapter
    from health_profile.service import HealthProfileService
    from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import actual_analysis_composition
    from tests.unit._support.current_document_profile import settle
    from tests.unit._support.current_report_projection import next_round

    original, correction = model_report(), model_report()
    correction['observations'][0]['resultValue'] = '44'
    flow = await actual_analysis_composition(monkeypatch, [original, correction])
    bundle = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/fixed-round.pdf', file_type='pdf'
    )
    await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(bundle),
        request_id='fixed-first-round',
        lifecycle_epoch=flow.epoch,
    )
    await settle(flow)
    await next_round(flow, bundle)
    monkeypatch.setattr(
        flow.upstream.index_collection, 'with_options', lambda **_: flow.upstream.index_collection, raising=False
    )
    archive = create_document_archived_extraction_reader(
        processing=flow.analysis,
        lifecycle=DocumentPublicationLifecycleReader(
            ledger=flow.ledger, snapshots=flow.prepared.understanding.snapshots
        ),
        current_source=flow.readers.source,
    )
    profile = HealthProfileService(flow.repository)
    profile.bind_document_archive_reader(ProfileDocumentArchiveAdapter(archive))
    scope = {'user_id': flow.scope.user_id, 'subject_id': flow.scope.subject_id}
    standard = await profile.list_document_observations(**scope)
    complete = await profile.list_document_observations(**scope, view='complete')
    assert standard['items'][0]['observation']['resultValue'] == '12'
    assert complete['items'][0]['observation']['resultValue'] == '44'
    assert complete['documents'][0]['sourceStatus'] == 'pending'
    reader = ProfileReportMaterialsReader(profiles=flow.profile.reader, archives=ProfileDocumentArchiveAdapter(archive))
    materials = await reader.read(**scope, expected_lifecycle_epoch=flow.epoch)
    assert [item['content']['rawValue'] for item in materials['materials']] == ['12']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['clinical', 'basic_only', 'unbound', 'complete', 'unresolved'])
async def test_current_archive_is_fixed_material_before_and_after_publication(monkeypatch, case):
    from adapters.analysis.archived_extractions import create_document_archived_extraction_reader
    from adapters.profile_document_archives import ProfileDocumentArchiveAdapter
    from modules.analysis.basic_info_managed import DocumentCurrentManagedCommand
    from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
    from modules.health_analysis.factors import HealthAnalysisGenerationError
    from modules.health_analysis.factors import personal_citations
    from modules.health_analysis.factors import personal_quote_units
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import actual_analysis_composition
    from tests.unit._support.current_document_profile import clinical_facts
    from tests.unit._support.current_document_profile import cycle
    from tests.unit._support.current_document_profile import settle

    complete = case == 'complete'
    quality = 'complete' if complete else 'partial'
    raw = model_report(incomplete=not complete)
    if case in {'basic_only', 'unresolved'}:
        raw['observations'] = []
    if case == 'unresolved':
        raw['unresolvedItems'] = [{'resultValue': 'Synthetic clinical text needing confirmation'}]
    flow = await actual_analysis_composition(monkeypatch, [raw, raw])
    bundle = await flow.prepared.prepare_bundle(
        scope=flow.scope,
        file_id='https://example.test/partial-material.pdf',
        file_type='pdf',
        lifecycle_epoch=flow.epoch if case != 'unbound' else None,
    )
    assert bundle.facts.payload['quality']['status'] == quality
    assert bundle.facts.payload['clinicalAccepted'] is complete
    monkeypatch.setattr(
        flow.upstream.index_collection, 'with_options', lambda **_: flow.upstream.index_collection, raising=False
    )
    archive = create_document_archived_extraction_reader(
        processing=flow.analysis,
        lifecycle=DocumentPublicationLifecycleReader(
            ledger=flow.ledger, snapshots=flow.prepared.understanding.snapshots
        ),
        current_source=flow.readers.source,
    )
    provider = ProfileReportMaterialsReader(
        profiles=flow.profile.reader, archives=ProfileDocumentArchiveAdapter(archive)
    )
    read = {'user_id': flow.scope.user_id, 'subject_id': flow.scope.subject_id, 'expected_lifecycle_epoch': flow.epoch}
    before = await provider.read(**read)
    assert before['status'] == ('available' if complete else 'partial')
    assert before['coverage']['complete'] is complete
    if case == 'unbound':
        assert not before['materials'] and not before['sourceSnapshot']['archives']
    expected_values = {
        'clinical': ['12'],
        'complete': ['12'],
        'unresolved': ['Synthetic clinical text needing confirmation'],
    }.get(case, [])
    if case == 'unresolved':
        (material,) = before['materials']
        assert material['metadata']['unresolved'] is True and material['status'] == 'unknown'
        quote = next(iter(personal_quote_units(material)))
        with pytest.raises(HealthAnalysisGenerationError, match='Unknown material'):
            personal_citations([{'evidenceId': material['id'], 'quote': quote}], {material['id']: material})
    assert [item['content'].get('resultValue') for item in before['materials']] == expected_values
    assert all(item['metadata']['materialQuality'] == quality for item in before['materials'])
    assert all(item['metadata']['publicationRevision'] is None for item in before['materials'])
    assert all(item['metadata']['verificationStatus'] == 'unconfirmed' for item in before['materials'])
    if case != 'unbound':
        await flow.runtime.consumption.consume_prepared(
            scope=flow.scope,
            prepared=flow.prepared.input(bundle),
            request_id='archive-publication',
            lifecycle_epoch=flow.epoch,
        )
        await settle(flow)
        after = await provider.read(**read)
        if complete:
            (published,) = after['materials']
            assert published['content']['rawValue'] == '12'
            assert published['metadata']['origin'] == 'source_contribution'
            assert published['metadata']['captureProofRef']
            assert await clinical_facts(flow)
            command = DocumentCurrentManagedCommand(
                flow.scope,
                bundle.material.source_id,
                'retire-archive-clinical',
                flow.epoch,
                1,
                published['metadata']['sourceSnapshotRef'],
                retire_contribution_ids=(published['metadata']['contributionId'],),
            )
            await flow.runtime.managed.execute(command)
            await settle(flow)
            retired = await provider.read(**read)
            (saved,) = retired['sourceSnapshot']['archives']
            assert saved['descriptor']['archive_managed_bindings'] == (
                (bundle.receipt.document_id, bundle.facts.payload['interpretationRound'], '/observations/0'),
            )
            assert saved['result']['observations'][0]['resultValue'] == '12'
            # Clinical retirement does not hide the immutable original or its
            # independently published demographics, but bars its contextual reuse.
            assert not retired['materials'] and not await clinical_facts(flow)
            assert (await flow.profile.basic_info.read(flow.scope.user_id)).fields['name'].value
        else:
            assert after['status'] == 'partial' and not after['coverage']['complete']
            assert [item['content']['rawValue'] for item in after['materials']] == expected_values
            facts = await clinical_facts(flow)
            assert len(facts) == len(expected_values)
            if expected_values:
                (retained,) = after['materials']
                (fact,) = facts
                metadata = retained['metadata']
                assert metadata['origin'] == 'source_contribution'
                assert metadata['captureProofRef'] and metadata['sourceSnapshotRef']
                assert metadata['materialQuality'] == 'partial' and metadata['publicationRevision'] == 1
                assert metadata['unresolved'] is (case == 'unresolved')
                assert metadata['sourcePointer'] == fact.sources[0].pointer
                assert fact.status == 'pending' and fact.verification_status == 'unconfirmed'
                assert personal_quote_units(retained) == expected_values and retained['status'] == 'unknown'
                with pytest.raises(HealthAnalysisGenerationError, match='Unknown material'):
                    personal_citations(
                        [{'evidenceId': retained['id'], 'quote': expected_values[0]}], {retained['id']: retained}
                    )
    assert len(flow.upstream.client.submit_calls) == 1 and len(flow.gateway.complete_requests) <= 2
    await cycle(flow)
    new_epoch = (
        await flow.profile.basic_info.read(flow.scope.user_id, subject_id=flow.scope.subject_id)
    ).lifecycle_epoch
    reused = await flow.prepared.prepare_bundle(
        scope=flow.scope,
        document_id=bundle.receipt.document_id,
        lifecycle_epoch=new_epoch,
        resume_incomplete=False,
    )
    assert reused.understanding.snapshot_id == bundle.understanding.snapshot_id
    (fixed,) = (await archive.list_archived(scope=flow.scope)).entries
    assert fixed.archive_lifecycle_epoch == (None if case == 'unbound' else flow.epoch)
    assert not (await provider.read(**{**read, 'expected_lifecycle_epoch': new_epoch}))['materials']


@pytest.mark.asyncio
async def test_fixed_pointer_retirement_preserves_independent_observation_from_the_same_round():
    current = descriptor(
        current_source=True,
        lifecycle_epoch=1,
        archive_lifecycle_epoch=1,
        archive_managed_bindings=(('document-1', 1, '/observations/0'),),
        processing={'status': 'partial'},
    )
    saved = {
        'observations': [observation('12'), observation('34')],
        'basicInfo': {'name': 'Synthetic user'},
    }
    provider, profiles, _ = setup([ProfileArchivedExtraction(current, saved)])
    profiles.resolve_existing_self_lifecycle = AsyncMock(return_value=('test-self', 1, 'active'))

    result = await provider.read(user_id='test-user', subject_id='test-self', expected_lifecycle_epoch=1)

    (material,) = result['materials']
    assert material['metadata']['sourcePointer'] == '/observations/1'
    assert material['content']['resultValue'] == '34'
    assert result['sourceSnapshot']['archives'][0]['result'] == saved


@pytest.mark.asyncio
async def test_current_partial_archive_rejects_an_unverified_fixed_result(monkeypatch):
    from adapters.analysis.archived_extractions import create_document_archived_extraction_reader
    from adapters.profile_document_archives import ProfileDocumentArchiveAdapter
    from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import actual_analysis_composition

    raw = model_report(incomplete=True)
    flow = await actual_analysis_composition(monkeypatch, [raw, raw])
    bundle = await flow.prepared.prepare_bundle(
        scope=flow.scope,
        file_id='https://example.test/changed-partial-material.pdf',
        file_type='pdf',
        lifecycle_epoch=flow.epoch,
    )
    record = flow.upstream.index_collection.documents[bundle.receipt.document_id]
    fixed = next(iter(record['archivedRounds'].values()))
    fixed['interpretationDigest'] = 'a' * 64
    monkeypatch.setattr(
        flow.upstream.index_collection, 'with_options', lambda **_: flow.upstream.index_collection, raising=False
    )
    archive = create_document_archived_extraction_reader(
        processing=flow.analysis,
        lifecycle=DocumentPublicationLifecycleReader(
            ledger=flow.ledger, snapshots=flow.prepared.understanding.snapshots
        ),
        current_source=flow.readers.source,
    )
    reader = ProfileReportMaterialsReader(profiles=flow.profile.reader, archives=ProfileDocumentArchiveAdapter(archive))
    result = await reader.read(
        user_id=flow.scope.user_id, subject_id=flow.scope.subject_id, expected_lifecycle_epoch=flow.epoch
    )
    assert result['status'] == 'partial' and not result['materials']
