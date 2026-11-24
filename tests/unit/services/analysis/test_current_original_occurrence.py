"""Actual publisher proof from fixed Report and Analysis archives."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from modules.analysis.basic_info_contracts import ANALYSIS_PUBLIC_CONFIRMATION
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.snapshots import canonical_json
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import install_profile
from tests.unit._support.current_document_profile import settle
from tests.unit._support.current_occurrence import page
from tests.unit._support.current_occurrence import readers_for
from tests.unit._support.current_report_projection import composition
from tests.unit._support.current_report_projection import next_round
from tests.unit._support.current_report_projection import projection


def publisher_receipt(event):
    # Profile's typed physicalOccurrence consumer is delivered in the Profile
    # PR. Verify actual Document publication material at its public receiver.
    from modules.analysis.publication import PublicationReceipt

    return PublicationReceipt(
        event['sourceId'],
        event['publicationRevision'],
        event['contentDigest'],
        'applied',
        'receipt-' + event['eventId'],
        application_state='applied',
    )


def wire(flow):
    from adapters.analysis.basic_info import create_document_basic_info_source_runtime
    from modules.analysis.publication_recovery import DocumentPublicationRecovery

    flow.readers = readers_for(flow)

    async def deliver(event):
        material = await flow.readers.source.read_source_observations(
            scope=flow.scope, snapshot_id=event['snapshotRef']['snapshotId']
        )
        assert material['sourceId'] == event['sourceId']
        return publisher_receipt(event)

    receiver = type('SyntheticReceiver', (), {})()
    receiver.deliver = AsyncMock(side_effect=deliver)
    receiver.lookup = AsyncMock(return_value=None)
    flow.runtime = create_document_basic_info_source_runtime(
        readers=flow.readers,
        publication_recovery=DocumentPublicationRecovery(
            receiver=receiver, ledger=flow.ledger, recovery_route='current', clock=lambda: flow.now[0]
        ),
    )


async def consume(flow, bundle, request_id, **options):
    result = await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(bundle),
        request_id=request_id,
        lifecycle_epoch=flow.epoch,
        **options,
    )
    assert result['publication']['state'] == 'applied'
    return await flow.readers.source.read_source_observations(scope=flow.scope, snapshot_id=result['sourceSnapshotRef'])


def occurrences(source, name):
    return [
        row['physicalOccurrence']
        for group in source['payload']['clinicalContributions']
        for row in group['observations']
        if row['label'] == name and 'physicalOccurrence' in row
    ]


def identity(proof):
    return tuple(proof[name] for name in ('documentId', 'sourceChecksum', 'locatorRevision', 'locator'))


@pytest.mark.asyncio
@pytest.mark.parametrize('order', ('record', 'report', 'parallel'))
async def test_same_fixed_original_identity_across_record_and_multi_page_report(monkeypatch, order):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    wire(flow)
    a, b = await page(flow, 'A'), await page(flow, 'B')
    group = await projection(flow, 'fixed-group', a, b)

    async def for_report():
        return await consume(flow, a, 'report-attach', report_projection_ref=group.snapshot_id)

    async def for_record():
        return await consume(flow, b, 'record-attach')

    if order == 'parallel':
        report, record = await asyncio.gather(for_report(), for_record())
    elif order == 'record':
        record, report = await for_record(), await for_report()
    else:
        report, record = await for_report(), await for_record()

    a_id, b_id = a.receipt.document_id, b.receipt.document_id
    assert report['sourceId'] != record['sourceId']
    report_alt, record_alt = occurrences(report, 'ALT'), occurrences(record, 'ALT')
    (record_b,) = record_alt
    assert [item['documentId'] for item in report_alt].count(a_id) == 2
    assert [item['documentId'] for item in report_alt].count(b_id) == 1
    assert len({identity(item) for item in report_alt + record_alt}) == 2
    (report_b,) = (item for item in report_alt if item['documentId'] == b_id)
    assert identity(report_b) == identity(record_b)
    assert report_b['proofSnapshotRef'] != record_b['proofSnapshotRef']
    assert len({identity(item) for item in occurrences(report, 'pH')}) == 4

    rebuilt = readers_for(flow)
    rebuilt.report_projections.materials.read_original = AsyncMock(side_effect=AssertionError('no new original read'))
    for original, source_id in ((report, a.material.source_id), (record, b.material.source_id)):
        ledger = await flow.ledger.read_source(flow.scope, source_id)
        saved = ledger['currentIntent']['event']['snapshotRef']['snapshotId']
        assert await rebuilt.source.read_source_observations(scope=flow.scope, snapshot_id=saved) == original


@pytest.mark.asyncio
async def test_proof_rejects_wrong_round_and_unknown_fixed_pointer_before_admission(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    wire(flow)
    a = await page(flow, 'A')
    newer = await next_round(flow, a)
    original = await projection(flow, 'original-singleton', a)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='exact singleton'):
        await flow.runtime.admission.prepare_source_plan(
            scope=flow.scope,
            prepared=flow.prepared.input(newer),
            request_id='wrong-round',
            lifecycle_epoch=flow.epoch,
            occurrence_projection_ref=original.snapshot_id,
        )
    assert await flow.ledger.read_source(flow.scope, a.material.source_id) is None

    payload = original.payload
    alt = next(row for row in payload['projection']['supplementaryHospitalData'] if row['itemName'] == 'ALT')
    alt['analysisObservationRefs'] = ['/observations/999']
    tampered = replace(original, _payload_json=canonical_json(payload))
    # Simulate a misbehaving upstream projector even if the archive can later
    # prove its immutable bytes. The referenced Analysis row must exist.
    selection, projector = flow.runtime.report_projections.selection, flow.runtime.report_projections.projector
    with monkeypatch.context() as fault:
        fault.setattr(
            flow.runtime.report_projections,
            'projector',
            AsyncMock(return_value=(selection, tampered.payload['projection'])),
        )
        malformed = await projection(flow, 'wrong-pointer', a)
        with pytest.raises(DocumentCompleteSourceUnavailable, match='invalid fixed Analysis pointer'):
            await flow.runtime.admission.prepare_source_plan(
                scope=flow.scope,
                prepared=flow.prepared.input(a),
                request_id='wrong-pointer',
                lifecycle_epoch=flow.epoch,
                occurrence_projection_ref=malformed.snapshot_id,
            )
    assert flow.runtime.report_projections.projector is projector
    assert await flow.ledger.read_source(flow.scope, a.material.source_id) is None


@pytest.mark.asyncio
async def test_fixed_occurrence_projection_is_shared_by_commands_and_legacy_material_remains_readable(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    a, b = await page(flow, 'A'), await page(flow, 'B')
    baseline = await projection(flow, 'before-opt-in', b)
    assert baseline.payload['manifest']['interpretationProjectionRevision'].endswith('.v2')
    wire(flow)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='exact singleton'):
        await flow.runtime.admission.prepare_source_plan(
            scope=flow.scope,
            prepared=flow.prepared.input(b),
            request_id='wrong-projection-revision',
            lifecycle_epoch=flow.epoch,
            occurrence_projection_ref=baseline.snapshot_id,
        )
    assert await flow.readers.report_projections.read(scope=flow.scope, snapshot_id=baseline.snapshot_id) == baseline
    assert await flow.ledger.read_source(flow.scope, b.material.source_id) is None

    material = flow.readers.report_projections.materials
    original = material.read_original
    material.read_original = AsyncMock(side_effect=original)
    await asyncio.gather(consume(flow, a, 'first-create'), consume(flow, a, 'second-create'))
    assert material.read_original.await_count == 1

    first = await consume(flow, b, 'old-report-projection', report_projection_ref=baseline.snapshot_id)
    # Historical explicit v2 submissions can still be read in a v3 runtime.
    # The v2 side does not acquire typed proof by inference.
    assert occurrences(first, 'ALT')
    report_rows = [
        row
        for group in first['payload']['clinicalContributions']
        for row in group['observations']
        if row['observationRef'].startswith('/reportSource/')
    ]
    assert report_rows and all('physicalOccurrence' not in row for row in report_rows)


@pytest.mark.asyncio
async def test_undated_original_uses_one_physical_identity_across_analysis_and_report(monkeypatch):
    raw = model_report()
    raw['reportTime'] = '2030-01-02'
    flow = await composition(monkeypatch, [raw], png=True)
    wire(flow)
    flow.upstream.client.result = {
        'success': True,
        'text': '受检者：测试甲\n姓名：测试甲\n性别：女\n年龄：42岁\nALT 12 U/L\n',
    }
    prepared = await page(flow, 'A')
    projected = await projection(flow, 'undated-singleton', prepared)
    original = next(
        row for row in projected.payload['projection']['supplementaryHospitalData'] if row['itemName'] == 'ALT'
    )
    assert original.get('reportTime') is None and original['analysisObservationRefs']
    material = await consume(flow, prepared, 'undated-alias', report_projection_ref=projected.snapshot_id)
    rows = [
        row
        for contribution in material['payload']['clinicalContributions']
        for row in contribution['observations']
        if row['label'] == 'ALT'
    ]
    assert len(rows) == 2
    assert {identity(row['physicalOccurrence']) for row in rows}
    assert len({identity(row['physicalOccurrence']) for row in rows}) == 1
    assert {(row['observedAt'], row['verificationStatus']) for row in rows} == {(None, 'unconfirmed')}
    ledger = await flow.ledger.read_source(flow.scope, prepared.material.source_id)
    snapshot_id = ledger['currentIntent']['event']['snapshotRef']['snapshotId']
    aliases = await flow.readers.source.read_fixed_observation_refs(scope=flow.scope, snapshot_id=snapshot_id)
    expected = {
        'documentId': prepared.receipt.document_id,
        'interpretationRound': prepared.facts.payload['interpretationRound'],
        'observationRef': '/observations/0',
    }
    assert all([expected] in [group.get(row['observationRef']) for group in aliases.values()] for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('report_first', (False, True))
async def test_public_confirmation_stays_on_the_new_analysis_publication(monkeypatch, report_first):
    flow = await composition(monkeypatch, [model_report()], png=True)
    wire(flow)
    bundle = await page(flow, 'A')
    material_reader = flow.readers.report_projections.materials
    original = material_reader.read_original
    material_reader.read_original = AsyncMock(side_effect=original)
    public = {'confirmation_basis': ANALYSIS_PUBLIC_CONFIRMATION}

    if report_first:
        report = await projection(flow, 'report-projection', bundle)
        original_material = await consume(flow, bundle, 'report', report_projection_ref=report.snapshot_id)
        assert await consume(flow, bundle, 'analysis:public', **public) == original_material
        assert material_reader.read_original.await_count == 1
    else:
        original_material = await consume(flow, bundle, 'analysis:public', **public)
        material_reader.read_original.assert_not_awaited()
        assert await consume(flow, bundle, 'record') == original_material
        report = await projection(flow, 'report-projection', bundle)
        assert material_reader.read_original.await_count == 1

    source_before_report = original_material['payload']['clinicalContributions']
    analysis_before = next(
        row
        for group in source_before_report
        for row in group['observations']
        if row['label'] == 'ALT' and not row['observationRef'].startswith('/reportSource/')
    )
    if report_first:
        assert analysis_before['verificationStatus'] == 'verified_source'
        assert 'confirmationBasis' not in analysis_before
    else:
        assert analysis_before['verificationStatus'] == 'confirmed'
        assert analysis_before['confirmationBasis'] == ANALYSIS_PUBLIC_CONFIRMATION
        assert 'physicalOccurrence' not in analysis_before

    with pytest.raises(ValueError, match='limited to direct current Analysis'):
        await consume(flow, bundle, 'report', report_projection_ref=report.snapshot_id, **public)
    current = await consume(flow, bundle, 'report', report_projection_ref=report.snapshot_id)
    ledger = await flow.ledger.read_source(flow.scope, bundle.material.source_id)
    snapshot_id = ledger['currentIntent']['event']['snapshotRef']['snapshotId']
    groups = current['payload']['clinicalContributions']
    rows = [
        (group['contributionId'], row) for group in groups for row in group['observations'] if row['label'] == 'ALT'
    ]
    assert len(rows) == 2
    report_id, report_alt = next(
        (group, row) for group, row in rows if row['observationRef'].startswith('/reportSource/')
    )
    assert 'confirmationBasis' not in report_alt
    assert report_alt['verificationStatus'] == 'verified_source'
    refs = await flow.readers.source.read_fixed_observation_refs(scope=flow.scope, snapshot_id=snapshot_id)
    (fixed_ref,) = refs[report_id][report_alt['observationRef']]
    assert fixed_ref == {
        'documentId': bundle.receipt.document_id,
        'interpretationRound': bundle.facts.payload['interpretationRound'],
        'observationRef': analysis_before['observationRef'],
    }
    if not report_first:
        (analysis_id,) = (group for group, row in rows if row.get('confirmationBasis'))
        assert refs[analysis_id][analysis_before['observationRef']] == [fixed_ref]
    assert ledger['publicationRevision'] == (1 if report_first else 2)


@pytest.mark.asyncio
async def test_public_confirmation_keeps_undated_results_and_delivers_unresolved_unchanged(monkeypatch):
    interpretation = model_report()
    interpretation.pop('reportTime')
    interpretation['observations'].append(
        {'itemName': 'Special', 'resultValue': 'trace', 'kind': 'measurement', 'unit': None}
    )
    interpretation['unresolvedItems'] = [{'itemName': 'AST', 'resultValue': None, 'reason': 'Missing value'}]
    interpretation['sections'] = [
        {
            'reportType': 'imaging',
            'expansion': {'findings': 'Observation in original interpretation', 'diagnosis': 'Imaging conclusion'},
        }
    ]
    flow = await composition(monkeypatch, [interpretation, interpretation], png=True)
    flow.upstream.client.result = {'success': True, 'text': 'Incomplete original results'}
    wire(flow)
    bundle = await page(flow, 'A')

    result = await consume(flow, bundle, 'analysis:undated', confirmation_basis=ANALYSIS_PUBLIC_CONFIRMATION)
    (clinical,) = result['payload']['clinicalContributions']
    rows = {row['label']: row for row in clinical['observations']}
    assert rows['ALT']['observedAt'] is None
    assert rows['ALT']['verificationStatus'] == 'confirmed'
    assert rows['Special']['verificationStatus'] == 'confirmed'
    assert rows['AST']['rawValue'] is None
    assert rows['AST']['verificationStatus'] == 'unconfirmed'
    assert 'confirmationBasis' not in rows['AST']
    imaging = {row['narrativeType']: row for row in rows.values() if row['narrativeType']}
    assert set(imaging) == {'findings', 'diagnosis'}
    assert all(row['verificationStatus'] == 'confirmed' for row in imaging.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('report_first', (False, True))
async def test_public_and_report_group_apply_one_fact_per_verified_original_position(monkeypatch, report_first):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    flow.readers = readers_for(flow)
    install_profile(flow)
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )
    a, b = await page(flow, 'A'), await page(flow, 'B')
    report = await projection(flow, 'report-group', a, b)

    requests = (
        (b, 'analysis:public', {'confirmation_basis': ANALYSIS_PUBLIC_CONFIRMATION}),
        (a, 'report', {'report_projection_ref': report.snapshot_id}),
    )
    for bundle, request, options in reversed(requests) if report_first else requests:
        result = await flow.runtime.consumption.consume_prepared(
            scope=flow.scope,
            prepared=flow.prepared.input(bundle),
            request_id=request,
            lifecycle_epoch=flow.epoch,
            **options,
        )
        assert result['publicationRevision'] == 1
        await settle(flow)

    alt = [row for row in await clinical_facts(flow) if row.display_name == 'ALT']
    assert len(alt) == 2
    stored = [await flow.repository.get_fact(flow.scope.user_id, flow.scope.subject_id, row.fact_id) for row in alt]
    first, second = sorted(stored, key=lambda row: len(row.physical_origins))
    assert len(first.physical_origins) == 1
    assert {origin.source_id for origin in second.physical_origins} == {a.material.source_id, b.material.source_id}
    assert (first.status, first.verification_status) == ('active', 'verified_source')
    assert (second.status, second.verification_status) == ('active', 'confirmed')


@pytest.mark.asyncio
async def test_public_confirmation_keeps_the_exact_child_reference_for_a_dated_display_mirror(monkeypatch):
    interpretation = model_report()
    child = {
        'reportType': 'lab',
        'reportTime': '2026/11/13',
        'observations': [{'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L', 'kind': 'measurement'}],
    }
    interpretation.update(reportType='microbiome', reportTime=None, observations=[], sections=[child])
    flow = await composition(monkeypatch, [interpretation], png=True)
    text = REPORT_TEXT.replace('ALT 12 U/L\n', '')
    text += '| 检验项目 | 结果 | 单位 | 检测日期 |\n'
    text += '| --- | --- | --- | --- |\n| ALT | 12 | U/L | 2026/11/13 |\n'
    flow.upstream.client.result = {'success': True, 'text': text}
    wire(flow)
    bundle = await page(flow, 'A')
    original_refs = {row['observationRef'] for row in bundle.facts.payload['observations']}
    root_pointer, child_pointer = '/observations/0', '/sections/0/observations/0'
    assert original_refs == {root_pointer, child_pointer}

    first = await consume(flow, bundle, 'analysis:mirror', confirmation_basis=ANALYSIS_PUBLIC_CONFIRMATION)
    (published,) = first['payload']['clinicalContributions']
    assert [row['observationRef'] for row in published['observations']] == [child_pointer]
    report = await projection(flow, 'report-mirror', bundle)
    original_report_row = next(
        row for row in report.payload['projection']['supplementaryHospitalData'] if row['itemName'] == 'ALT'
    )
    assert set(original_report_row['analysisObservationRefs']) == original_refs
    result = await consume(flow, bundle, 'report', report_projection_ref=report.snapshot_id)
    ledger = await flow.ledger.read_source(flow.scope, bundle.material.source_id)
    snapshot_id = ledger['currentIntent']['event']['snapshotRef']['snapshotId']
    refs = await flow.readers.source.read_fixed_observation_refs(scope=flow.scope, snapshot_id=snapshot_id)
    for contribution in result['payload']['clinicalContributions']:
        (row,) = [row for row in contribution['observations'] if row['label'] == 'ALT']
        (verified_ref,) = refs[contribution['contributionId']][row['observationRef']]
        assert verified_ref['observationRef'] == child_pointer
    assert {row['observationRef'] for row in bundle.facts.payload['observations']} == original_refs
