"""Document Report correction, page expansion and per-occurrence retirement."""

import pytest

from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.publication import PublicationConflictError
from modules.analysis.snapshots import digest
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_report_projection import SAME_VALUE_ROWS
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


async def prepare_page(flow, name, *, extra=''):
    if name != 'A':
        flow.upstream.downloader.download.return_value = page_bytes(f'Synthetic page {name}', png=True)
        flow.upstream.client.result = {
            'success': True,
            'text': '续页 检测结果\n' + REPORT_TEXT + SAME_VALUE_ROWS + extra,
        }
    return await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id=f'https://example.test/page-{name}.png', file_type='png'
    )


def model_with_value(value):
    model = model_report()
    model['observations'][0]['resultValue'] = value
    return model


async def inventory(flow, document_source):
    return await flow.readers.source.read_source_inventory(
        scope=flow.scope,
        snapshot_id=document_source['payload']['coverage']['inventorySnapshotRef']['snapshotId'],
    )


def origins(report):
    return {
        item['stableObservationId']: (item['evidence'][0]['sourceDocumentId'], item['evidence'][0]['sourceOccurrence'])
        for item in report['observations']
    }


def report_facts(facts, report):
    prefix = f'/contributions/{report["contributionId"]}/reportSource/'
    return [fact for fact in facts if any((entry.pointer or '').startswith(prefix) for entry in fact.sources)]


@pytest.mark.asyncio
async def test_corrected_report_can_return_to_earlier_content_with_new_round_identity(monkeypatch):
    flow = await composition(
        monkeypatch, [model_report(), model_report(), model_with_value('15'), model_report()], png=True
    )
    primary = await prepare_page(flow, 'A')
    secondary = await prepare_page(flow, 'B', extra='ALT 15 U/L\n')
    initial_projection = await projection(flow, 'report-X1', primary, secondary)
    initial_ref = await admit(flow, primary, initial_projection)
    await publish(flow)
    (first,) = report_contributions(await source(flow, initial_ref))

    changed_secondary = await next_round(flow, secondary)
    changed = await projection(flow, 'report-Y2', primary, changed_secondary)
    changed_req = command(
        flow,
        primary,
        initial_ref,
        1,
        'replace-y',
        report_projection_ref=changed.snapshot_id,
        replace_report_contribution_id=first['contributionId'],
    )
    active, receipt = await apply(flow, changed_req)
    (current,) = report_contributions(active)
    assert current['clinicalContentDigest'] != first['clinicalContentDigest']

    restored_secondary = await next_round(flow, changed_secondary)
    restored = await projection(flow, 'report-X3', primary, restored_secondary)
    restore_req = command(
        flow,
        primary,
        receipt['sourceSnapshotRef'],
        2,
        'replace-x',
        report_projection_ref=restored.snapshot_id,
        replace_report_contribution_id=current['contributionId'],
    )
    active, receipt = await apply(flow, restore_req)
    (updated,) = report_contributions(active)
    assert updated['clinicalContentDigest'] == first['clinicalContentDigest']
    assert len({first['contributionId'], current['contributionId'], updated['contributionId']}) == 3
    retired = (await inventory(flow, active))['payload']['retiredContributions']
    assert {row['contributionId'] for row in retired if row['retirementKind'] == 'corrected'} == {
        first['contributionId'],
        current['contributionId'],
    }
    assert report_contributions(await source(flow, initial_ref)) == (first,)
    wire_projection(flow)
    assert await flow.runtime.managed.execute(restore_req) == receipt
    assert receipt['publicationRevision'] == 3
    duplicate = await projection(flow, 'report-X3-new-receipt', primary, restored_secondary)
    no_change = command(
        flow, primary, receipt['sourceSnapshotRef'], 3, 'same-current', report_projection_ref=duplicate.snapshot_id
    )
    assert (await flow.runtime.managed.execute(no_change))['status'] == 'unchanged'
    assert report_contributions(await source(flow, receipt['sourceSnapshotRef'])) == (updated,)


@pytest.mark.asyncio
async def test_explicitly_expand_report_prefix_preserving_distinct_original_observations(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report()], png=True)
    primary = await prepare_page(flow, 'A')
    continuation = await prepare_page(flow, 'B')
    one_page = await projection(flow, 'only-a', primary)
    original_ref = await admit(flow, primary, one_page)
    await publish(flow)
    initial = await source(flow, original_ref)
    (old_report,) = report_contributions(initial)
    direct = next(row for row in initial['payload']['clinicalContributions'] if row != old_report)
    both_pages = await projection(flow, 'a-then-b', primary, continuation)

    from_datastore = await flow.ledger.read_source(flow.scope, primary.material.source_id)
    with pytest.raises(PublicationConflictError, match='overlaps'):
        await flow.runtime.managed.execute(
            command(flow, primary, original_ref, 1, 'overlap', report_projection_ref=both_pages.snapshot_id)
        )
    with pytest.raises(PublicationConflictError, match='exact active contribution'):
        await flow.runtime.managed.execute(
            command(
                flow,
                primary,
                original_ref,
                1,
                'wrong-target',
                report_projection_ref=both_pages.snapshot_id,
                replace_report_contribution_id=direct['contributionId'],
            )
        )
    assert await flow.ledger.read_source(flow.scope, primary.material.source_id) == from_datastore
    extension = command(
        flow,
        primary,
        original_ref,
        1,
        'expand',
        report_projection_ref=both_pages.snapshot_id,
        replace_report_contribution_id=old_report['contributionId'],
    )
    data, receipt = await apply(flow, extension)
    (report,) = report_contributions(data)
    assert origins(old_report).items() < origins(report).items()
    ph = [row for row in report['observations'] if row['label'] == 'pH']
    assert len(ph) == len({row['stableObservationId'] for row in ph}) == 4
    assert {row['evidence'][0]['sourceDocumentId'] for row in ph} == {
        primary.receipt.document_id,
        continuation.receipt.document_id,
    }
    assert {row['rawValue'] for row in ph} == {'7.1'}
    assert direct in data['payload']['clinicalContributions']
    facts = await clinical_facts(flow)
    assert not report_facts(facts, old_report)
    assert len([fact for fact in report_facts(facts, report) if fact.display_name == 'pH']) == 4
    retired = (await inventory(flow, data))['payload']['retiredContributions']
    assert [(row['retirementKind'], row['supersededByContributionId']) for row in retired] == [
        ('corrected', report['contributionId'])
    ]
    with pytest.raises(PublicationConflictError, match='ordered extension'):
        await flow.runtime.managed.execute(
            command(
                flow,
                primary,
                receipt['sourceSnapshotRef'],
                2,
                'shrink',
                report_projection_ref=one_page.snapshot_id,
                replace_report_contribution_id=report['contributionId'],
            )
        )
    assert await flow.runtime.managed.execute(extension) == receipt
    assert report_contributions(await source(flow, original_ref)) == (old_report,)


@pytest.mark.asyncio
async def test_explicit_retirement_filters_unchanged_page_and_publishes_changed_page(monkeypatch):
    flow = await composition(monkeypatch, [model_report(), model_report(), model_with_value('15')], png=True)
    primary = await prepare_page(flow, 'A')
    secondary = await prepare_page(flow, 'B', extra='ALT 15 U/L\n')
    grouped = await projection(flow, 'a-b', primary, secondary)
    original_ref = await admit(flow, primary, grouped)
    await publish(flow)
    (previous,) = report_contributions(await source(flow, original_ref))
    retire = command(flow, primary, original_ref, 1, 'retire', retire_contribution_ids=(previous['contributionId'],))
    after_retire, retirement = await apply(flow, retire)
    assert not report_contributions(after_retire)
    assert not report_facts(await clinical_facts(flow), previous)

    unchanged_group = await projection(flow, 'a-b-new-receipt', primary, secondary)
    restore = command(
        flow,
        primary,
        retirement['sourceSnapshotRef'],
        2,
        'unchanged',
        report_projection_ref=unchanged_group.snapshot_id,
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='all Report occurrences are retired'):
        await flow.runtime.managed.execute(restore)
    assert await flow.ledger.read_reserved(flow.scope, primary.material.source_id, restore.command_id) is None
    regrouped = await projection(flow, 'a-only', primary)
    regroup = command(
        flow, primary, retirement['sourceSnapshotRef'], 2, 'regroup', report_projection_ref=regrouped.snapshot_id
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='all Report occurrences are retired'):
        await flow.runtime.managed.execute(regroup)
    assert await flow.ledger.read_reserved(flow.scope, primary.material.source_id, regroup.command_id) is None

    updated_secondary = await next_round(flow, secondary)
    new_digest = updated_secondary.facts.payload['clinicalContentDigest']
    assert new_digest != secondary.facts.payload['clinicalContentDigest']
    revised = await projection(flow, 'a-b-updated', primary, updated_secondary)
    rows = revised.payload['projection']['supplementaryHospitalData']
    assert {row.get('sourceDocumentId') for row in rows} >= {primary.receipt.document_id, secondary.receipt.document_id}
    correction = command(
        flow, primary, retirement['sourceSnapshotRef'], 2, 'only-new-page', report_projection_ref=revised.snapshot_id
    )
    active, receipt = await apply(flow, correction)
    (report,) = report_contributions(active)
    assert report['clinicalContentDigest'] == digest([(secondary.receipt.document_id, new_digest)])
    assert {document for document, _ in origins(report).values()} == {secondary.receipt.document_id}
    for row in report['observations']:
        evidence = row['evidence'][0]
        assert evidence['artifactRef'] == revised.snapshot_id
        index = int(evidence['pointer'].removeprefix('/payload/projection/supplementaryHospitalData/'))
        fixed = revised.payload['projection']['supplementaryHospitalData'][index]
        assert fixed['sourceOccurrence'] == evidence['sourceOccurrence']
    facts = await clinical_facts(flow)
    assert not report_facts(facts, previous)
    assert len([fact for fact in report_facts(facts, report) if fact.display_name == 'pH']) == 2
    rows = (await inventory(flow, active))['payload']['retiredContributions']
    assert [row['contributionId'] for row in rows if row['retirementKind'] == 'retracted'] == [
        previous['contributionId']
    ]
    wire_projection(flow)
    assert await flow.readers.report_projections.read(scope=flow.scope, snapshot_id=revised.snapshot_id) == revised
    assert report_contributions(await source(flow, original_ref)) == (previous,)
    assert await flow.runtime.managed.execute(correction) == receipt
