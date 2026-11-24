"""Actual Document/Profile public material composition with offline dependencies."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from adapters.profile_document_archives import ProfileDocumentArchiveAdapter
from health_profile.report_materials import ProfileReportMaterialsReader
from health_profile.snapshots import ProfileSnapshotReader
from modules.analysis.application.document_reuse import _CheckpointTrace
from tests.unit._support.document_profile_composition import apply
from tests.unit._support.document_profile_composition import combined_source


def public_reader(current, reader):
    return ProfileReportMaterialsReader(
        profiles=ProfileSnapshotReader(current.profile.repository, publications=current.profile.inbox),
        archives=ProfileDocumentArchiveAdapter(reader),
        page_size=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['retire', 'withdraw'])
async def test_actual_mixed_contributions_keep_fixed_refs_and_lifecycle_without_any_processing(action):
    current, _, archive, command, published = await combined_source()
    reader = public_reader(current, archive)
    query = {'user_id': current.scope.user_id, 'subject_id': current.scope.subject_id}
    calls = (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls))
    profile_before = deepcopy({name: coll.documents for name, coll in current.profile.collections.items()})
    before = await reader.read(**query)
    assert before['materials'] and before['coverage']['mirrorsRemoved'] > 0
    assert len({row['id'] for row in before['materials']}) == len(before['materials'])
    supplementary = [row for row in before['materials'] if row['metadata']['origin'] == 'source_contribution']
    assert supplementary
    for row in supplementary:
        assert row['metadata']['sourceSnapshotRef']
        assert row['metadata']['contributionSnapshotRef']
        assert row['metadata']['captureProofRef']
        assert row['metadata']['materialSnapshotRefs']
        assert len(row['metadata']['contributionContentDigest']) == 64
    for row in before['sourceSnapshot']['archives']:
        assert row['descriptor']['material_ref'] and row['descriptor']['result_ref']
        assert len(row['descriptor']['result_digest']) == 64
    assert {name: coll.documents for name, coll in current.profile.collections.items()} == profile_before
    assert (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls)) == calls

    if action == 'retire':
        await current.runtime.commands.execute(
            replace(
                command,
                request_id='report-material-retire',
                operation='retire_report',
                prior_publication_revision=published['publicationRevision'],
                prior_source_snapshot_ref=published['sourceSnapshotRef'],
            )
        )
    else:
        await current.runtime.publisher.retract(
            scope=current.scope,
            source_id=command.source_id,
            source_revision=current.bundle.material.source_revision,
            request_id='report-material-withdraw',
            expected_prior_revision=published['publicationRevision'],
        )
    await apply(current)
    calls = (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls))
    after = await reader.read(**query)
    assert all(
        row['metadata']['origin'] != 'source_contribution' or row['metadata']['canonicalReportId'] != command.source_id
        for row in after['materials']
    )
    assert (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls)) == calls


@pytest.mark.asyncio
async def test_actual_missing_new_archive_keeps_old_effective_contribution_identity(monkeypatch):
    current, _, archive, _, _ = await combined_source()
    reader = public_reader(current, archive)
    query = {'user_id': current.scope.user_id, 'subject_id': current.scope.subject_id}
    before = await reader.read(**query)
    old_contributions = {
        row['metadata']['contributionSnapshotRef']
        for row in before['materials']
        if row['metadata']['origin'] == 'source_contribution'
    }
    document_id = current.bundle.material.payload['documentId']
    traces, trace_id = archive.traces, 'report-material-new-pending'
    result = deepcopy(current.bundle.understanding.payload['interpretation'])
    result['observations'][0]['resultValue'] = '477'
    await traces.create({'_id': trace_id, **current.scope.as_dict()})
    await traces.write_payload(trace_id, current.scope.user_id, 'interpretation', result)
    document = current.env.flow.index_collection.documents[document_id]
    reuse = current.env.flow.service._document_reuse
    document.update(
        interpretationRound=2,
        interpretationResultRound=2,
        status='running',
        leaseToken='new-archive-owner',
        leaseExpiresAt=reuse.now() + timedelta(minutes=2),
    )
    document['payloadRefs'] = {**document['payloadRefs'], 'interpretation': trace_id}
    checkpoint = _CheckpointTrace(reuse, document, 'new-archive-owner', SimpleNamespace(trace_id=trace_id))
    await checkpoint.archive_round(result)
    await checkpoint.save(status='completed', leaseToken=None, leaseExpiresAt=None)
    read_payload = traces.read_payload

    async def interrupted(identity, user_id, subject_id, name):
        if identity == trace_id and name == 'interpretation':
            raise RuntimeError('synthetic temporary new archive outage')
        return await read_payload(identity, user_id, subject_id, name)

    monkeypatch.setattr(traces, 'read_payload', interrupted)
    calls = (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls))
    unavailable = await reader.read(**query)
    assert unavailable['status'] == 'partial'
    snapshots = [
        row for row in unavailable['sourceSnapshot']['archives'] if row['descriptor']['document_id'] == document_id
    ]
    assert snapshots[0]['result'] is None and snapshots[0]['descriptor']['interpretation_round'] == 2
    surviving = [row for row in unavailable['materials'] if row['metadata']['origin'] == 'source_contribution']
    assert surviving and old_contributions <= {row['metadata']['contributionSnapshotRef'] for row in surviving}
    assert all(row['metadata']['extractionId'] != snapshots[0]['descriptor']['extraction_id'] for row in surviving)
    assert (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls)) == calls
