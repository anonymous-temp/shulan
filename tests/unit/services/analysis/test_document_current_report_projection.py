"""Current Report projection through the actual Analysis, Document and Profile providers."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.current_report_contribution import report_occurrence_observations
from modules.analysis.publication import PublicationConflictError
from modules.analysis.snapshots import CURRENT_REPORT_PROJECTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from modules.report import parsing
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import UPLOAD
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import actual_analysis_composition
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_report_projection import admit
from tests.unit._support.current_report_projection import apply
from tests.unit._support.current_report_projection import command
from tests.unit._support.current_report_projection import composition
from tests.unit._support.current_report_projection import next_round
from tests.unit._support.current_report_projection import page_bytes
from tests.unit._support.current_report_projection import projection
from tests.unit._support.current_report_projection import report_contributions
from tests.unit._support.current_report_projection import source
from tests.unit._support.current_report_projection import wire_projection


@pytest.mark.asyncio
async def test_fixed_report_projection_recovers_ack_loss_and_publishes_distinct_original_rows(monkeypatch):
    flow = await composition(monkeypatch, [model_report()])
    primary = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    duplicate = await flow.prepared.prepare_bundle(scope=flow.scope, document_id=primary.receipt.document_id)
    assert duplicate.receipt.reused and duplicate.receipt.trace_id != primary.receipt.trace_id
    assert not any(row['label'] == 'pH' for row in primary.facts.payload['observations'])

    native_extract = Mock(wraps=parsing._extract_pdf)
    monkeypatch.setattr(parsing, '_extract_pdf', native_extract)
    original_put = flow.readers.report_projections.snapshots.put

    async def lose_projection_ack(snapshot):
        await original_put(snapshot)
        if snapshot.contract_version == CURRENT_REPORT_PROJECTION_CONTRACT:
            raise OSError('synthetic projection acknowledgement loss')

    with monkeypatch.context() as interrupted:
        interrupted.setattr(flow.readers.report_projections.snapshots, 'put', lose_projection_ack)
        with pytest.raises(OSError, match='projection acknowledgement loss'):
            await projection(flow, 'report-occurrences', primary, duplicate)
    native_extract.assert_called_once()
    wire_projection(flow)

    async def must_not_read_original(**kwargs):
        pytest.fail('fixed projection must not read original bytes again')

    monkeypatch.setattr(flow.readers.report_projections.materials, 'read_original', must_not_read_original)
    archived = await projection(flow, 'report-occurrences', primary, duplicate)
    assert archived.contract_version == CURRENT_REPORT_PROJECTION_CONTRACT
    assert archived.payload['projectionDigest'] == digest(archived.payload['projection'])
    assert archived.payload['processingStatus'] == 'complete'
    (dependency,) = archived.payload['dependencies']
    assert dependency['documentId'] == primary.receipt.document_id
    assert dependency['clinicalContentDigest'] == primary.facts.payload['clinicalContentDigest']
    assert (await flow.readers.report_projections.read(scope=flow.scope, snapshot_id=archived.snapshot_id)) == archived
    other = DocumentScope(flow.scope.user_id, 'other-subject')
    with pytest.raises(DocumentNotFoundError):
        await flow.readers.report_projections.read(scope=other, snapshot_id=archived.snapshot_id)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='lifecycle'):
        await flow.readers.report_projections.prepare(
            scope=flow.scope,
            inputs=(flow.prepared.input(primary), flow.prepared.input(duplicate)),
            request_id='report-occurrences',
            lifecycle_epoch=flow.epoch + 1,
        )

    ref = await admit(flow, primary, archived)
    publication = await publish(flow)
    intent = await flow.ledger.read_reserved(flow.scope, primary.material.source_id, 'publication-first')
    profile_scope = await flow.profile.reader.resolve_scope(flow.scope.user_id, subject_id=flow.scope.subject_id)
    profile_receipt = await flow.profile.inbox.get_receipt(intent['event']['eventId'], scope=profile_scope)
    assert profile_receipt.publication_id == publication.receipt_ref and profile_receipt.status == 'applied'
    (report,) = report_contributions(await source(flow, ref))
    assert report['kind'] == 'clinical' and report['basicInfo'] == []
    ph = [row for row in report['observations'] if row['label'] == 'pH']
    assert len(ph) == 2
    assert {row['rawValue'] for row in ph} == {'7.1'}
    assert {(row['specimen'], row['method']) for row in ph} == {('粪便', '试纸法'), ('尿液', '电极法')}
    assert len({row['stableObservationId'] for row in ph}) == 2
    assert len({row['evidence'][0]['sourceOccurrence'] for row in ph}) == 2
    for row in ph:
        proof = row['evidence'][0]
        assert proof['artifactRef'] == archived.snapshot_id
        assert proof['sourceDocumentId'] == primary.receipt.document_id
        assert proof['sourceChecksum'] == primary.material.payload['contentSha256']
    assert (await info(flow)).fields['age'].value == 42
    facts = await clinical_facts(flow)
    assert any(row.display_name == 'ALT' for row in facts)
    assert any(
        origin.pointer.startswith(f'/contributions/{report["contributionId"]}/reportSource/')
        for fact in facts
        for origin in fact.sources
        if origin.pointer is not None
    )
    assert len(flow.gateway.complete_requests) == flow.upstream.downloader.download.await_count == 1
    native_extract.assert_called_once()


@pytest.mark.asyncio
async def test_partial_report_projection_keeps_analysis_observations_and_its_own_archived_quality(monkeypatch):
    flow = await composition(monkeypatch, [model_report(incomplete=True), model_report(incomplete=True)])
    partial = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    archived = await projection(flow, 'partial', partial)
    assert partial.facts.payload['quality']['status'] == archived.payload['processingStatus'] == 'partial'
    assert archived.payload['projection']['processing']['status'] == 'partial'
    ref = await admit(flow, partial, archived)
    await publish(flow)
    material = await source(flow, ref)
    assert not report_contributions(material)
    [clinical] = material['payload']['clinicalContributions']
    assert {row['verificationStatus'] for row in clinical['observations']} == {'unconfirmed'}
    assert (await info(flow)).fields['age'].value == 42
    assert any(row.display_name == 'ALT' and row.status == 'pending' for row in await clinical_facts(flow))


@pytest.mark.asyncio
async def test_report_contribution_append_replace_replay_and_dependency_retirement(monkeypatch):
    changed_secondary, changed_primary = model_report(), model_report()
    changed_secondary['observations'][0]['resultValue'] = '15'
    changed_primary['observations'][0]['resultValue'] = '14'
    primary_basic_only = model_report()
    primary_basic_only['basicInfoMeta']['gender']['sourceReliability'] = 'medium'
    flow = await composition(
        monkeypatch,
        [model_report(), model_report(), changed_secondary, primary_basic_only, changed_primary],
        png=True,
    )
    primary = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/first-page.png', file_type='png'
    )
    flow.upstream.downloader.download.return_value = page_bytes('Synthetic secondary', png=True)
    flow.upstream.client.result = {'success': True, 'text': '续页 检测结果\n' + REPORT_TEXT + 'AST 15 U/L\n'}
    secondary = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/secondary.png', file_type='png'
    )
    first = await projection(flow, 'primary-and-secondary', primary, secondary)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='different primary preparation'):
        await admit(flow, secondary, first)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='source lifecycle'):
        await flow.runtime.admission.prepare_source_plan(
            scope=flow.scope,
            prepared=flow.prepared.input(primary),
            request_id='wrong-lifecycle',
            lifecycle_epoch=flow.epoch + 1,
            report_projection_ref=first.snapshot_id,
        )
    for bundle in (primary, secondary):
        assert await flow.ledger.read_source(flow.scope, bundle.material.source_id) is None
    original_ref = await admit(flow, primary)
    await publish(flow)
    assert not report_contributions(await source(flow, original_ref))

    append = command(flow, primary, original_ref, 1, 'append', report_projection_ref=first.snapshot_id)
    appended, receipt = await apply(flow, append)
    (prior_report,) = report_contributions(appended)
    assert receipt['publicationRevision'] == 2
    assert await flow.runtime.managed.execute(append) == receipt
    assert [row['documentId'] for row in first.payload['dependencies']] == [
        primary.receipt.document_id,
        secondary.receipt.document_id,
    ]

    duplicate = await projection(flow, 'same-inputs-new-receipt', primary, secondary)
    assert duplicate.snapshot_id != first.snapshot_id
    assert duplicate.payload['projectionDigest'] == first.payload['projectionDigest']
    same = command(
        flow, primary, receipt['sourceSnapshotRef'], 2, 'unchanged-append', report_projection_ref=duplicate.snapshot_id
    )
    same_result = await flow.runtime.managed.execute(same)
    assert same_result['status'] == 'unchanged' and same_result['sourceSnapshotRef'] == receipt['sourceSnapshotRef']

    secondary_correction = await next_round(flow, secondary)
    assert (
        secondary_correction.facts.payload['clinicalContentDigest'] != secondary.facts.payload['clinicalContentDigest']
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='conflict'):
        await projection(flow, 'conflicting-rounds', primary, secondary, secondary_correction)
    replacement = await projection(flow, 'secondary-corrected', primary, secondary_correction)
    replace_request = command(
        flow,
        primary,
        receipt['sourceSnapshotRef'],
        2,
        'replace',
        report_projection_ref=replacement.snapshot_id,
        replace_report_contribution_id=prior_report['contributionId'],
    )
    replaced, replacement_receipt = await apply(flow, replace_request)
    (current_report,) = report_contributions(replaced)
    assert current_report['contributionId'] != prior_report['contributionId']
    assert current_report['clinicalContentDigest'] != prior_report['clinicalContentDigest']
    assert replacement_receipt['publicationRevision'] == 3

    with pytest.raises((DocumentCompleteSourceUnavailable, PublicationConflictError)):
        await flow.runtime.managed.execute(
            replace(
                replace_request,
                request_id='replace-retired',
                prior_publication_revision=3,
                prior_source_snapshot_ref=replacement_receipt['sourceSnapshotRef'],
            )
        )

    basic_correction = await next_round(flow, primary)
    assert basic_correction.facts.payload['clinicalContentDigest'] == primary.facts.payload['clinicalContentDigest']
    kept, basic_receipt = await apply(
        flow,
        command(
            flow,
            primary,
            replacement_receipt['sourceSnapshotRef'],
            3,
            'basic-only-change',
            understanding_snapshot_ref=basic_correction.understanding.snapshot_id,
        ),
    )
    assert report_contributions(kept) == (current_report,)

    clinical_correction = await next_round(flow, basic_correction)
    assert (
        clinical_correction.facts.payload['clinicalContentDigest']
        != basic_correction.facts.payload['clinicalContentDigest']
    )
    retired, retirement = await apply(
        flow,
        command(
            flow,
            primary,
            basic_receipt['sourceSnapshotRef'],
            4,
            'clinical-change',
            understanding_snapshot_ref=clinical_correction.understanding.snapshot_id,
        ),
    )
    assert retirement['publicationRevision'] == 5
    assert not report_contributions(retired)
    inventory_ref = retired['payload']['coverage']['inventorySnapshotRef']['snapshotId']
    inventory = await flow.readers.source.read_source_inventory(scope=flow.scope, snapshot_id=inventory_ref)
    assert {row['contributionId'] for row in inventory['payload']['retiredContributions']} >= {
        prior_report['contributionId'],
        current_report['contributionId'],
    }
    assert report_contributions(await source(flow, replacement_receipt['sourceSnapshotRef'])) == (current_report,)


@pytest.mark.asyncio
async def test_explicit_report_retirement_survives_a_new_projection_request(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    primary = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/first-page.png', file_type='png'
    )
    earlier = await projection(flow, 'first-projection', primary)
    initial = await admit(flow, primary, earlier)
    await publish(flow)
    (report,) = report_contributions(await source(flow, initial))
    retired, receipt = await apply(
        flow,
        command(
            flow,
            primary,
            initial,
            1,
            'retire-report',
            retire_contribution_ids=(report['contributionId'],),
        ),
    )
    assert receipt['publicationRevision'] == 2
    assert not report_contributions(retired)
    before = await flow.ledger.read_source(flow.scope, primary.material.source_id)

    fresh = await projection(flow, 'fresh-projection', primary)
    assert fresh.snapshot_id != earlier.snapshot_id
    assert fresh.payload['dependencies'] == earlier.payload['dependencies']
    replay = command(
        flow, primary, receipt['sourceSnapshotRef'], 2, 'resurrect', report_projection_ref=fresh.snapshot_id
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='retired contributions'):
        await flow.runtime.managed.execute(replay)
    assert await flow.ledger.read_reserved(flow.scope, primary.material.source_id, replay.command_id) is None

    assert await flow.ledger.read_source(flow.scope, primary.material.source_id) == before
    assert await source(flow, receipt['sourceSnapshotRef']) == retired
    assert report_contributions(await source(flow, initial)) == (report,)


@pytest.mark.parametrize(
    ('data_type', 'value', 'unit', 'expected'),
    [
        ('INSPECTION', '420 10^9/L', None, ('420', '10^9/L')),
        ('INSPECTION', '≤ 4.2 ×10^9/L', None, ('≤ 4.2', '×10^9/L')),
        ('INSPECTION', '4.2×10^9/L', None, ('4.2', '×10^9/L')),
        ('INSPECTION', '42010^9/L', None, ('42010^9/L', None)),
        ('INSPECTION', '420 10⁹/L', None, ('420', '10⁹/L')),
        ('INSPECTION', '420 10^9/L', '10^9/L', ('420', '10^9/L')),
        ('INSPECTION', '420 10^9/L', 'U/L', ('420 10^9/L', 'U/L')),
        ('INSPECTION', '12 U/L', None, ('12', 'U/L')),
        ('INSPECTION', '12U/L', None, ('12', 'U/L')),
        ('INSPECTION', '4.2 mmol / L', None, ('4.2', 'mmol / L')),
        ('INSPECTION', '1.2e3 mg/dL', None, ('1.2e3', 'mg/dL')),
        ('INSPECTION', '1e3', None, ('1e3', None)),
        ('INSPECTION', '1,420 10^9/L', None, ('1,420', '10^9/L')),
        ('INSPECTION', '4.5%', None, ('4.5', '%')),
        ('INSPECTION', '3–5 10^9/L', None, ('3–5 10^9/L', None)),
        ('INSPECTION', '2 positive', None, ('2 positive', None)),
        ('EXAM', '420 10^9/L', None, ('420 10^9/L', None)),
    ],
)
def test_report_measurement_separates_only_unambiguous_units(data_type, value, unit, expected):
    key = 'testResultValue' if data_type == 'INSPECTION' else 'examResult'
    row = {
        'dataType': data_type,
        'itemName': 'PLT',
        key: value,
        'unit': unit,
        'sourceDocumentId': 'synthetic',
        'sourceChecksum': 'a' * 64,
        'sourceOccurrence': 'synthetic-cell',
    }
    (observation,) = report_occurrence_observations(
        {'supplementaryHospitalData': [row]},
        projection_ref='synthetic-projection',
        dependencies=[{'documentId': 'synthetic', 'contentSha256': 'a' * 64}],
    )
    assert (observation['rawValue'], observation['unit']) == expected
    assert row[key] == value


@pytest.mark.asyncio
async def test_report_platelet_measurement_with_scaled_unit_is_applied_as_quantity(monkeypatch):
    text = (
        REPORT_TEXT
        + """
| 检验项目 | 检验结果 | 单位 | 检验日期 |
| --- | --- | --- | --- |
| PLT | 420 | 10^9/L | 2026-11-12 |
"""
    )
    flow = await actual_analysis_composition(
        monkeypatch, [model_report()], text=text, native_content=page_bytes('Synthetic platelets')
    )
    flow.report_admissions = flow.readers.records.collection
    wire_projection(flow)
    primary = await flow.prepared.prepare_bundle(scope=flow.scope, **UPLOAD)
    archived = await projection(flow, 'platelets', primary)
    ref = await admit(flow, primary, archived)
    await publish(flow)
    (contribution,) = report_contributions(await source(flow, ref))
    (observation,) = (row for row in contribution['observations'] if row['label'] == 'PLT')
    assert (observation['rawValue'], observation['unit']) == ('420', '10^9/L')
    (fixed_row,) = (
        row for row in archived.payload['projection']['supplementaryHospitalData'] if row['itemName'] == 'PLT'
    )
    assert fixed_row['testResultValue'] == '420 10^9/L'
    (fact,) = (fact for fact in await clinical_facts(flow) if fact.display_name == 'PLT')
    assert fact.value_type == 'quantity'
    assert (fact.value['number'], fact.value['unit']) == (420, '10^9/L')
    assert fact.sources[0].pointer == f'/contributions/{contribution["contributionId"]}{observation["observationRef"]}'
