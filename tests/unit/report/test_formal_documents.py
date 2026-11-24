"""Exact Report formal consumption, with no second private extraction."""

from dataclasses import asdict
from dataclasses import replace

import pytest
from bson import BSON

from adapters.report.formal_documents import create_formal_report_document_source_factory
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.service import HealthReportUpstreamError
from modules.analysis.managed_source_contracts import MANAGED_REPORT_REVISION
from modules.analysis.snapshots import MANAGED_REPORT_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.report.document_projection import REPORT_PREPARED_PROJECTION_REVISION
from modules.report.ports import DocumentServiceError
from modules.report.schemas import RemoteFileInput
from tests.unit.report.test_fixed_documents import SOURCE
from tests.unit.report.test_fixed_documents import environment as fixed_environment


class Formal:
    def __init__(self, env):
        self.env, self.calls, self.outputs = env, [], {}
        self.status = 'pending'
        self.after_execute = False
        self.foreign = False

    async def submit(self, command):
        self.calls.append(('submit', command))
        return self.receipt(command)

    async def prepare_report(self, command):
        self.calls.append(('prepare', command))
        if command.command_id not in self.outputs:
            bundle = self.env.bundle
            self.outputs[command.command_id] = DocumentSnapshot(
                command.scope,
                MANAGED_REPORT_CONTRACT,
                bundle.material.source_id,
                bundle.material.source_revision,
                canonical_json(
                    {
                        'manifest': {
                            'scope': command.scope.as_dict(),
                            'inputs': [x.as_dict() for x in command.inputs],
                            'revision': MANAGED_REPORT_REVISION,
                            'interpretationProjectionRevision': 'document-understanding-public.v1',
                        },
                        'selection': asdict(self.env.factory.selection),
                        'projection': {'subject': {'name': 'fixed public v16'}},
                        'processingStatus': 'complete',
                        'outputTraceId': 'actual-preparation-trace',
                    }
                ),
            )
        return self.outputs[command.command_id]

    def receipt(self, command):
        projection = self.outputs.get(command.command_id)
        bases = (
            [{'kind': 'not_published', 'sourceId': self.env.bundle.material.source_id}]
            if self.status == 'not_published'
            else []
        )
        return {
            'commandId': 'another-command' if self.foreign else command.command_id,
            'commandRevision': command.command_revision,
            'status': self.status,
            'phase': 'report',
            'completedBaseCount': len(bases),
            'totalUniqueSources': 1,
            'result': {
                'basePublications': bases,
                'projectionRef': projection.snapshot_id if projection else None,
                'processingStatus': 'complete',
                'reportPublication': None,
            },
        }

    async def execute(self, command):
        self.calls.append(('execute', command))
        if self.after_execute:
            raise RuntimeError('ACK lost after durable source work')
        return self.receipt(command)


async def environment():
    env = await fixed_environment()
    env.formal = Formal(env)
    env.factory = create_formal_report_document_source_factory(
        compatibility=env.compatibility,
        materials=env.materials,
        formal=env.formal,
        projections=env.projections,
        policy=env.factory.policy,
    )
    env.gateway = env.factory.create(
        user_id=env.bundle.material.scope.user_id, session_id='session', final_report_id='final'
    )
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'failed', 'not_published'])
async def test_source_state_does_not_change_business_quality_or_call_second_extractor(status):
    env = await environment()
    env.formal.status = status
    result = await env.gateway.read(SOURCE, index=0)
    assert result.parsed.subject.name == 'fixed public v16'
    ref = result.references[0]
    assert ref['processingStatus'] == 'completed'
    assert ref['formalSource']['status'] == status
    assert ref['sourceRevision'] is None and ref['jobId'] is None
    assert ref['outputTraceId'] == 'actual-preparation-trace'
    assert [x[0] for x in env.formal.calls] == ['submit', 'prepare', 'execute']
    assert await env.gateway.read(SOURCE, index=0) == result
    assert len(env.formal.calls) == 3
    env.extractor.assert_not_awaited()
    assert not env.publications.calls
    manifest = env.factory.document_input_manifest()
    assert manifest['consumerRevision'] == 'report-formal-documents.v1'
    assert manifest['preparedProjectionRevision'] == REPORT_PREPARED_PROJECTION_REVISION


@pytest.mark.asyncio
async def test_unknown_execution_ack_keeps_actual_fixed_projection_and_durable_command_identity():
    env = await environment()
    env.formal.after_execute = True
    result = await env.gateway.read(SOURCE, index=0)
    assert result.references[0]['formalSource']['status'] == 'unknown'
    assert result.references[0]['processingStatus'] == 'completed'
    assert {call.command_id for _, call in env.formal.calls} == {result.references[0]['formalSource']['commandId']}
    env.extractor.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_receipt_rejected_without_using_its_source_identity():
    env = await environment()
    env.formal.foreign = True
    with pytest.raises(DocumentServiceError, match='receipt'):
        await env.gateway.read(SOURCE, index=0)


@pytest.mark.asyncio
async def test_fixed_projection_binding_change_rejected_before_formal_execution():
    env = await environment()
    original = env.formal.prepare_report

    async def wrong(command):
        snapshot = await original(command)
        payload = snapshot.payload
        payload['manifest']['inputs'][0]['materialSnapshotRef'] = 'another-material'
        return replace(snapshot, _payload_json=canonical_json(payload))

    env.formal.prepare_report = wrong
    with pytest.raises(DocumentServiceError, match='fixed Report inputs'):
        await env.gateway.read(SOURCE, index=0)
    assert 'execute' not in [phase for phase, _ in env.formal.calls]


@pytest.mark.asyncio
async def test_submit_ack_loss_reuses_same_prepared_bundle_and_command():
    env = await environment()
    original = env.formal.submit
    first = True

    async def lost(command):
        nonlocal first
        result = await original(command)
        if first:
            first = False
            raise RuntimeError('submit ACK lost')
        return result

    env.formal.submit = lost
    with pytest.raises(RuntimeError, match='submit ACK'):
        await env.gateway.read(SOURCE, index=0)
    env.compatibility.prepare_bundle.side_effect = AssertionError('must not recapture')
    await env.gateway.read(SOURCE, index=0)
    commands = [x for phase, x in env.formal.calls if phase == 'submit']
    assert commands[0] == commands[1]
    env.compatibility.prepare_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_snapshot_private_cache_never_reexecutes_provider_or_consumer_extractor():
    env = await environment()
    await env.gateway.read(SOURCE, index=0)
    source = env.gateway.processor.prepared[0].source
    snapshot = next(iter(env.formal.outputs.values()))
    env.extractor.side_effect = AssertionError('no second extraction')
    data = await env.projections.project_prepared((source,), selection=env.factory.selection, projection=snapshot)
    assert data['subject']['name'] == 'fixed public v16'
    changed = replace(source, _manifest_json=canonical_json({**source.manifest, 'factsSnapshotRef': 'other'}))
    with pytest.raises(ValueError, match='different fixed inputs'):
        await env.projections.project_prepared((changed,), selection=env.factory.selection, projection=snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'revision,allowed', [('analysis-health-report-v5', True), ('analysis-health-report-v6', False)]
)
async def test_formal_failed_material_keeps_old_fallback_version_and_never_submits(revision, allowed):
    env = await environment()
    env.compatibility.prepare_bundle.side_effect = DocumentInterpretationError('fixed interpretation failure')
    payload = env.bundle.material.payload
    payload.update(
        interpretationRevision=revision,
        validationRevision=revision,
        failureTrace={'traceId': 'fixed-failed', 'errorType': 'DocumentInterpretationError'},
    )
    env.materials.capture_failed_material.return_value = replace(
        env.bundle.material, _payload_json=canonical_json(payload)
    )
    if allowed:
        result = await env.gateway.read(SOURCE, index=0)
        assert result.references[0]['processingStatus'] == 'fallback_source_only'
        assert env.extractor.call_args.args[0][0].report == {}
    else:
        with pytest.raises(HealthReportUpstreamError):
            await env.gateway.read(SOURCE, index=0)
    assert not env.formal.calls


@pytest.mark.asyncio
async def test_mixed_raw_group_keeps_order_duplicates_and_no_formal_group_command():
    env = await environment()
    await env.gateway.read(SOURCE, index=0)
    env.compatibility.prepare_bundle.side_effect = DocumentInterpretationError('failed second page')
    payload = env.bundle.material.payload
    payload['documentMetadata']['documentId'] = 'failed-other'
    payload['documentId'] = 'failed-other'
    payload.update(
        interpretationRevision='analysis-health-report-v5',
        validationRevision='analysis-health-report-v5',
        failureTrace={'traceId': 'fixed-second', 'errorType': 'DocumentInterpretationError'},
    )
    env.materials.capture_failed_material.return_value = replace(
        env.bundle.material, _payload_json=canonical_json(payload)
    )
    await env.gateway.read(RemoteFileInput(url='https://example.test/second.pdf', file_type='pdf'), index=1)
    before = len(env.formal.calls)
    combined = await env.gateway.combine([0, 1, 1])
    executed = env.extractor.call_args.args[0]
    assert [x.document_id for x in executed] == [env.source.document_id, 'failed-other', 'failed-other']
    assert all(x.report == {} for x in executed)
    assert len(env.formal.calls) == before == 3
    assert [x['fileIndex'] for x in combined.references] == [0, 1, 1]
    assert all(x['groupProcessingStatus'] == 'fallback_source_only' for x in combined.references)


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', [2**40, True, 1.0, 0, 2**53])
async def test_actual_bson_archive_hash_is_preserved_but_invalid_revisions_are_rejected(revision):
    env = await environment()

    async def acknowledged(command):
        value = env.formal.receipt(command)
        value['completedBaseCount'] = 1
        value['result']['basePublications'] = [
            {
                'kind': 'legacy',
                'sourceId': env.bundle.material.source_id,
                'canonicalReportId': env.bundle.material.source_id,
                'legacySourceRevision': revision,
                'ingestionStatus': 'pending',
                'jobId': None,
            }
        ]
        return BSON.encode(value).decode()

    env.formal.execute = acknowledged
    if type(revision) is int and revision == 2**40:
        result = await env.gateway.read(SOURCE, index=0)
        actual = result.references[0]['sourceRevision']
        assert type(actual) is int and actual == revision
        assert result.references[0]['ingestionStatus'] == 'pending'
    else:
        with pytest.raises(DocumentServiceError, match='archive identity'):
            await env.gateway.read(SOURCE, index=0)
