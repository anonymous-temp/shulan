"""Formal v2 reuses verified preparation; v1 and legacy source identity remain executable."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.output_profiles import DocumentSnapshot as ExtractorInput
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.formal_source_contracts import FORMAL_REPORT_REUSE_REVISION
from modules.analysis.formal_source_contracts import FORMAL_SOURCE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from modules.report.document_source import extract_intrinsic_report_source
from tests.unit.services.analysis.test_document_formal_source import advance
from tests.unit.services.analysis.test_document_formal_source import build
from tests.unit.services.analysis.test_document_formal_source import flow
from tests.unit.services.analysis.test_document_formal_source import request


async def legacy_flow():
    current = await flow()
    await current.env.api.publish_prepared(
        scope=current.scope,
        understanding_snapshot_id=current.item.understanding_snapshot_ref,
        selection=current.item.selection,
        preparation_receipt=current.item.preparation_receipt,
    )
    profiles = current.env.publisher.coordinator.profiles
    original = profiles.get(LEGACY_REPORT_SOURCE_SELECTION)
    current.extractions = []
    current.change_output = False

    async def counted(inputs):
        current.extractions.append(deepcopy(inputs))
        output = original.extractor(inputs)
        if hasattr(output, '__await__'):
            output = await output
        if current.change_output:
            output = {**deepcopy(output), 'sourceSummary': 'a different fixed prepared result'}
        return output

    profiles._profiles[LEGACY_REPORT_SOURCE_SELECTION] = replace(original, extractor=counted)
    current.command = replace(request(current, operation='report'), command_revision=FORMAL_REPORT_REUSE_REVISION)
    return current


@pytest.mark.asyncio
@pytest.mark.parametrize('version,expected', [(FORMAL_SOURCE_REVISION, 2), (FORMAL_REPORT_REUSE_REVISION, 1)])
async def test_explicit_v2_runs_extractor_once_while_v1_keeps_its_execution(version, expected):
    current = await legacy_flow()
    command = replace(current.command, command_revision=version)
    prepared = await current.formal.prepare_report(command)
    result = await current.formal.execute(command)
    assert result['status'] == 'completed', result
    assert len(current.extractions) == expected
    publication = result['result']['reportPublication']
    assert publication['kind'] == 'legacy_report' and publication['legacySourceRevision'] > 1
    assert result['result']['projectionRef'] == prepared.snapshot_id
    assert await current.formal.execute(command) == result
    assert len(current.extractions) == expected


@pytest.mark.asyncio
async def test_v2_preserves_old_cache_source_key_opaque_revision_and_actual_trace():
    current = await legacy_flow()
    prepared = await current.formal.prepare_report(current.command)
    result = await current.formal.execute(current.command)
    assert result['status'] == 'completed', result
    publication = result['result']['reportPublication']
    old = await current.env.publisher.publish_report_source(
        scope=current.scope,
        inputs=current.command.inputs,
        selection=LEGACY_REPORT_SOURCE_SELECTION,
    )
    assert old.legacy_source_revision == publication['legacySourceRevision']
    assert len(current.extractions) == 1
    output = await current.env.publisher.read_projection(
        scope=current.scope,
        projection_ref=old.projection_ref,
        selection=LEGACY_REPORT_SOURCE_SELECTION,
    )
    assert output.payload['extractionId'] == publication['extractionId']
    cached = await current.env.publisher.coordinator.index.get(
        publication['extractionId'],
        current.scope.user_id,
        current.scope.subject_id,
    )
    assert cached['preparedOutput'] == {
        'projectionRef': prepared.snapshot_id,
        'outputTraceId': prepared.payload['outputTraceId'],
    }
    assert 'output_raw' not in cached['payloadRefs']
    assert cached['payloadRefs']['output_result'] == publication['outputTraceId']
    assert prepared.payload['outputTraceId'] != publication['outputTraceId']
    trace = current.env.traces.documents[publication['outputTraceId']]
    assert trace['preparedProjectionRef'] == prepared.snapshot_id
    assert trace['preparedOutputTraceId'] == prepared.payload['outputTraceId']
    assert trace['outputReuseRevision'] == 'document-formal-prepared-v16-v1'
    assert 'sourceRevision' in trace['preparedInputExcludedArchiveFields']
    assert trace['status'] == 'completed'
    snapshot = await current.env.publisher.snapshots.get(current.scope, publication['projectionRef'])
    assert snapshot.payload['binding']['revision'] == 'formal-fixed-bases-v2'
    assert snapshot.payload['binding']['preparedProjectionRef'] == prepared.snapshot_id
    assert snapshot.payload['projection'] == prepared.payload['projection']


@pytest.mark.asyncio
async def test_existing_equal_legacy_cache_is_reused_without_replacing_its_provenance():
    current = await legacy_flow()
    old = await current.env.publisher.publish_report_source(
        scope=current.scope,
        inputs=current.command.inputs,
        selection=LEGACY_REPORT_SOURCE_SELECTION,
    )
    projected = await current.formal.prepare_report(current.command)
    result = await current.formal.execute(current.command)
    assert result['status'] == 'completed', result
    assert len(current.extractions) == 2  # One historical extraction, one business preparation.
    publication = result['result']['reportPublication']
    assert publication['legacySourceRevision'] == old.legacy_source_revision
    record = await current.env.publisher.coordinator.index.get(
        publication['extractionId'],
        current.scope.user_id,
        current.scope.subject_id,
    )
    assert 'preparedOutput' not in record
    assert await current.formal.read_report_projection(scope=current.scope, projection_ref=projected.snapshot_id)


@pytest.mark.asyncio
async def test_existing_different_cache_fails_source_but_preserves_fixed_business_projection():
    current = await legacy_flow()
    old = await current.env.publisher.publish_report_source(
        scope=current.scope,
        inputs=current.command.inputs,
        selection=LEGACY_REPORT_SOURCE_SELECTION,
    )
    current.change_output = True
    prepared = await current.formal.prepare_report(current.command)
    before = deepcopy(current.env.flow.output_index._collection.documents)
    result = await current.formal.execute(current.command)
    assert result['status'] == 'failed' and result['errorCode'] == 'OutputCacheConflictError'
    assert result['result']['projectionRef'] == prepared.snapshot_id
    assert current.env.flow.output_index._collection.documents == before
    assert len(current.extractions) == 2
    assert (await current.env.publisher.read_publication(scope=current.scope, receipt_ref=old.receipt_ref)) == old
    assert (
        await current.formal.read_report_projection(scope=current.scope, projection_ref=prepared.snapshot_id)
        == prepared
    )


@pytest.mark.asyncio
async def test_changed_legacy_clinical_content_is_not_seeded_into_old_cache(monkeypatch):
    current = await legacy_flow()
    prepared = await current.formal.prepare_report(current.command)
    original = current.env.publisher.read_legacy_revision

    async def changed(*args, **kwargs):
        record = await original(*args, **kwargs)
        altered = deepcopy(record.result)
        altered['observations'][0]['resultValue'] = 'different old accepted value'
        return replace(record, result=altered)

    monkeypatch.setattr(current.env.publisher, 'read_legacy_revision', changed)
    result = await current.formal.execute(current.command)
    assert result['status'] == 'failed' and result['errorCode'] == 'DocumentFormalSourceConflict'
    assert result['result']['projectionRef'] == prepared.snapshot_id and len(current.extractions) == 1
    assert not current.env.flow.output_index._collection.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['save', 'checkpoint', 'receipt'])
async def test_unknown_ack_reuses_prepared_output_without_a_second_extraction(monkeypatch, phase):
    current = await legacy_flow()
    prepared = await current.formal.prepare_report(current.command)
    coordinator = current.env.publisher.coordinator
    if phase == 'save':
        target, method = current.env.flow.source, 'save_revision'
    elif phase == 'checkpoint':
        target, method = coordinator.index, 'update_owned'
    else:
        target, method = current.runtime.commands.records, 'put'
    original = getattr(target, method)
    fired = [False]

    async def lost(*args, **kwargs):
        value = await original(*args, **kwargs)
        eligible = (
            phase == 'save'
            or (phase == 'checkpoint' and args[3].get('status') == 'completed')
            or (phase == 'receipt' and args[0] == 'formal_legacy_report_result')
        )
        if eligible and not fired[0]:
            fired[0] = True
            raise RuntimeError('persisted ACK lost')
        return value

    monkeypatch.setattr(target, method, lost)
    await current.formal.submit(current.command)
    await current.formal.advance(current.command.command_id)
    current.formal = build(current)
    result = await advance(current, current.command)
    assert result['status'] == 'completed', result
    assert fired[0] and len(current.extractions) == 1
    assert result['result']['projectionRef'] == prepared.snapshot_id


def test_actual_fixed_v16_does_not_read_archive_identity_fields():
    report = {
        'reportType': 'lab',
        'reportTime': '2026-11-10',
        'observations': [{'itemName': 'glucose', 'resultValue': '6.4', 'unit': 'mmol/L'}],
    }
    original = ExtractorInput('doc', report, 'glucose 6.4 mmol/L', 'lab.jpg', 'image', 'a' * 64)
    annotated = replace(
        original,
        report={
            **deepcopy(report),
            'reportId': 'old',
            'userId': 'user',
            'subjectId': 'self',
            'documentId': 'doc',
            'sourceRevision': 91234567890,
        },
    )
    assert extract_intrinsic_report_source((original,)) == extract_intrinsic_report_source((annotated,))


def test_v2_is_report_only_and_unknown_versions_do_not_select_latest():
    from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
    from tests.unit.services.analysis.test_document_snapshots import SCOPE

    with pytest.raises(UnsupportedOutputVersionError):
        DocumentFormalSourceCommand(SCOPE, 'x', 'base', (), FORMAL_REPORT_REUSE_REVISION)
    with pytest.raises(UnsupportedOutputVersionError):
        DocumentFormalSourceCommand(SCOPE, 'x', 'report', (), 'document-formal-source-v99')


@pytest.mark.asyncio
async def test_two_formal_commands_share_cas_cache_and_actual_opaque_revision():
    current = await legacy_flow()
    await current.formal.prepare_report(current.command)
    other = replace(current.command, request_id='another-business')
    results = await asyncio.gather(current.formal.execute(current.command), current.formal.execute(other))
    assert all(result['status'] == 'completed' for result in results), results
    assert len({result['result']['reportPublication']['legacySourceRevision'] for result in results}) == 1
    assert len(current.extractions) == 1 and len(current.env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_different_prepared_manifest_is_rejected_before_legacy_cache_write():
    current = await legacy_flow()
    prepared = await current.formal.prepare_report(current.command)
    changed = replace(current.command, inputs=(current.item, current.item))
    with pytest.raises(DocumentFormalSourceConflict, match='prepared output differs'):
        await current.formal.steps.legacy_reports.publish_prepared(
            command=changed,
            bases=(),
            projection_ref=prepared.snapshot_id,
        )
    assert not current.env.flow.output_index._collection.documents and len(current.extractions) == 1


@pytest.mark.asyncio
async def test_fixed_original_hash_is_rechecked_without_extraction_fallback(monkeypatch):
    current = await legacy_flow()
    prepared = await current.formal.prepare_report(current.command)
    monkeypatch.setattr(
        current.env.publisher.materials, 'read_original', AsyncMock(return_value=b'not the saved original')
    )
    result = await current.formal.execute(current.command)
    assert result['status'] != 'completed' and len(current.extractions) == 1
    assert result['result']['projectionRef'] == prepared.snapshot_id
    assert not current.env.flow.output_index._collection.documents
    assert not current.env.flow.reports._revisions.documents


@pytest.mark.asyncio
async def test_late_prepared_payload_cannot_publish_after_cache_lease_is_stolen(monkeypatch):
    current = await legacy_flow()
    await current.formal.prepare_report(current.command)
    trace_store = current.env.flow.service._document_trace_store
    original = trace_store.write_payload
    stolen = []

    async def changed(trace_id, user_id, name, payload):
        result = await original(trace_id, user_id, name, payload)
        if name == 'output_result':
            record = next(iter(current.env.flow.output_index._collection.documents.values()))
            record['leaseToken'] = 'successor'
            stolen.append(record['_id'])
        return result

    monkeypatch.setattr(trace_store, 'write_payload', changed)
    await current.formal.submit(current.command)
    result = await current.formal.advance(current.command.command_id)
    assert result['status'] == 'pending' and stolen
    cached = current.env.flow.output_index._collection.documents[stolen[0]]
    assert cached['leaseToken'] == 'successor' and 'output_result' not in cached['payloadRefs']
    assert not current.env.flow.reports._revisions.documents and len(current.extractions) == 1
