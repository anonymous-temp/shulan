"""Fixed current Report projection, immutable replay and recovery."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from adapters.analysis.source_admission import MongoDocumentAdmissionStore
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.contracts import OutputSelection
from modules.analysis.current_preparation import DocumentPreparedService
from modules.analysis.current_report_projection import DocumentCurrentReportProjection
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from tests.unit._support.current_document import bson_collection
from tests.unit._support.current_document import environment

SELECTION = OutputSelection('report_source', '1', 'synthetic-fixed-report-projection')
REVISION = 'synthetic-interpretation-v1'


@pytest_asyncio.fixture
async def flow():
    env = await environment(status='partial', edit=lambda raw: raw['processing'].update(retryable=False, issueCount=2))
    reader = SimpleNamespace(validate_prepared=AsyncMock(return_value=env.bundle))
    materials = SimpleNamespace(read_original=AsyncMock(return_value=b'synthetic PDF bytes'))
    collection = bson_collection()
    records = MongoDocumentAdmissionStore(collection)
    inputs = (DocumentPreparedService.input(env.original),)
    payload = {
        'metrics': [],
        'sourceDocuments': [{'documentId': 'synthetic-doc', 'sourceName': 'synthetic.pdf', 'sourceChecksum': 'a' * 64}],
        'processing': {'status': 'partial', 'retryable': False, 'issueCount': 2},
    }
    projector = AsyncMock(return_value=(SELECTION, payload))

    def service(*, selection=SELECTION, revision=REVISION, execute=projector):
        return DocumentCurrentReportProjection(
            prepared=reader,
            materials=materials,
            snapshots=env.store,
            records=records,
            projector=execute,
            selection=selection,
            interpretation_projection_revision=revision,
        )

    return SimpleNamespace(
        service=service,
        args=dict(scope=env.scope, inputs=inputs, request_id='synthetic-projection', lifecycle_epoch=1),
        snapshots=env.store,
        materials=materials,
        collection=collection,
        records=records,
        projector=projector,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_concurrent_instances_bind_one_receipt_and_snapshot(flow):
    reached = asyncio.Event()
    calls = []

    async def project(inputs):
        calls.append(inputs)
        if len(calls) == 2:
            reached.set()
        await reached.wait()
        return SELECTION, flow.payload

    first, second = await asyncio.wait_for(
        asyncio.gather(
            flow.service(execute=project).prepare(**flow.args), flow.service(execute=project).prepare(**flow.args)
        ),
        timeout=3,
    )
    assert first == second
    assert len(calls) == 2 and calls[0] == calls[1]
    assert first.payload['projectionDigest'] == digest(flow.payload)
    assert len(flow.collection.documents) == 2  # One immutable command and one receipt.
    changed = {**flow.args, 'lifecycle_epoch': 2}
    with pytest.raises(DocumentCompleteSourceUnavailable, match='inputs or lifecycle differ'):
        await flow.service().prepare(**changed)
    flow.projector.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_read_and_command_replay_skip_original_and_live_projector_even_after_upgrade(flow, monkeypatch):
    original = await flow.service().prepare(**flow.args)
    new_projector = AsyncMock(side_effect=AssertionError('fixed output should never be executed again'))
    restarted = flow.service(
        execute=new_projector,
        selection=replace(SELECTION, implementation_revision='synthetic-v2'),
        revision='adapter-v2',
    )
    flow.materials.read_original.side_effect = AssertionError('pure read should not reopen raw originals')
    monkeypatch.setattr(flow.snapshots, 'put', AsyncMock(side_effect=AssertionError('pure read should not write')))
    monkeypatch.setattr(flow.records, 'put', AsyncMock(side_effect=AssertionError('pure read should not write')))
    read = dict(scope=flow.args['scope'], snapshot_id=original.snapshot_id)
    assert await restarted.read(**read) == original == await restarted.prepare(**flow.args)
    with pytest.raises(DocumentNotFoundError):
        await restarted.read(**{**read, 'scope': DocumentScope('another-user', 'another-subject')})
    new_projector.assert_not_awaited()
    assert flow.materials.read_original.await_count == 1


@pytest.mark.asyncio
async def test_missing_snapshot_after_receipt_recovers_only_the_same_revisions_and_output(flow, monkeypatch):
    put = flow.snapshots.put
    monkeypatch.setattr(flow.snapshots, 'put', AsyncMock(side_effect=OSError('snapshot write interrupted')))
    with pytest.raises(OSError, match='interrupted'):
        await flow.service().prepare(**flow.args)
    assert len(flow.collection.documents) == 2
    original_attempt = flow.snapshots.put.await_args.args[0]
    monkeypatch.setattr(flow.snapshots, 'put', put)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='original Report execution revisions'):
        await flow.service(revision='adapter-v2').prepare(**flow.args)
    flow.projector.assert_awaited_once()
    bad = AsyncMock(return_value=(SELECTION, {**flow.payload, 'metrics': [{'value': 99}]}))
    with pytest.raises(DocumentCompleteSourceUnavailable, match='immutable source admission proof conflicts'):
        await flow.service(execute=bad).prepare(**flow.args)
    with pytest.raises(DocumentNotFoundError):
        await flow.service().read(scope=flow.args['scope'], snapshot_id=original_attempt.snapshot_id)
    assert await flow.service().prepare(**flow.args) == original_attempt
    assert flow.projector.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['selection', 'quality', 'retryable', 'issue count', 'order'])
async def test_wrong_report_result_never_binds_an_execution_receipt(flow, mismatch):
    selection = SELECTION
    payload = {**flow.payload, 'processing': dict(flow.payload['processing'])}
    if mismatch == 'selection':
        selection = replace(SELECTION, implementation_revision='wrong-revision')
    elif mismatch == 'quality':
        payload['processing']['status'] = 'unavailable'
    elif mismatch == 'retryable':
        payload['processing']['retryable'] = 1
    elif mismatch == 'issue count':
        payload['processing']['issueCount'] = -1
    else:
        payload['sourceDocuments'] = []
    invalid = AsyncMock(return_value=(selection, payload))
    with pytest.raises(DocumentCompleteSourceUnavailable, match='actual Report'):
        await flow.service(execute=invalid).prepare(**flow.args)
    assert len(flow.collection.documents) == 1
    assert (await flow.service().prepare(**flow.args)).payload['projection'] == flow.payload
    assert len(flow.collection.documents) == 2


@pytest.mark.asyncio
async def test_fixed_projection_read_rejects_conflicting_digest_and_unbound_output(flow):
    service = flow.service()
    fixed = await service.prepare(**flow.args)
    payload = fixed.payload
    altered = {**payload, 'projection': {**payload['projection'], 'metrics': [{'value': 99}]}}
    for value, expected in (
        (altered, 'output digest differs'),
        ({**altered, 'projectionDigest': digest(altered['projection'])}, 'execution binding differs'),
    ):
        fault = replace(fixed, _payload_json=canonical_json(value))
        await flow.snapshots.put(fault)
        with pytest.raises(DocumentCompleteSourceUnavailable, match=expected):
            await service.read(scope=flow.args['scope'], snapshot_id=fault.snapshot_id)
    flow.projector.assert_awaited_once()
