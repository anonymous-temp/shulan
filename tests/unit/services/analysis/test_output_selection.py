"""Version routing, historical input identity and durable extension compatibility."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.output_profiles import DocumentOutputCoordinator
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.analysis.application.output_profiles import OutputProfile
from modules.analysis.contracts import OutputCacheConflictError
from modules.analysis.contracts import OutputRegistrationConflictError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import extended_flow

V1 = OutputSelection('report_source', '1', 'test-report-source-v1')
V2 = OutputSelection('report_source', '2', 'test-report-source-v2')


def add_v2(flow):
    newer = AsyncMock(return_value={**deepcopy(flow.extractor.return_value), 'sourceSummary': 'new projection'})
    flow.service.output_profiles.register_report_source(
        newer, version=V2.implementation_revision, contract_version=V2.contract_version, legacy=False
    )
    return newer


def test_full_version_registration_never_rebinds_legacy_alias():
    registry = DocumentOutputProfiles()
    old, new = lambda _: {}, lambda _: {}
    registry.register(OutputProfile('custom', 'impl-a', old))
    registry.register(OutputProfile('custom', 'impl-b', new), legacy=False)
    registry.register(OutputProfile('custom', 'impl-a', new, contract_version='2'), legacy=False)
    assert registry.get('custom').extractor is old
    assert registry.get(OutputSelection('custom', '1', 'impl-b')).extractor is new
    assert registry.get(OutputSelection('custom', '2', 'impl-a')).extractor is new
    with pytest.raises(OutputRegistrationConflictError):
        registry.register(OutputProfile('custom', 'impl-a', new), legacy=False)
    with pytest.raises(OutputRegistrationConflictError):
        registry.register(OutputProfile('custom', 'impl-c', new))
    with pytest.raises(UnsupportedOutputVersionError):
        registry.get(OutputSelection('custom', '1', 'impl-c'))
    assert registry.get('custom').version == 'impl-a'


@pytest.mark.parametrize('value', [None, '', ' ', '1 ', 1])
def test_selection_requires_exact_nonempty_identifiers(value):
    with pytest.raises(ValueError):
        OutputSelection('report_source', value, 'impl-a')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['process_document', 'process_documents'])
async def test_unsupported_version_is_rejected_before_download_or_parsing(method):
    flow = extended_flow()
    selection = OutputSelection('report_source', '1', 'missing')
    with pytest.raises(UnsupportedOutputVersionError):
        if method == 'process_documents':
            await flow.service.process_documents(
                [{'url': 'https://example.com/a.pdf'}], user_id='user-1', output_selection=selection
            )
        else:
            await flow.service.process_document(**request(output_selection=selection))
    flow.downloader.download.assert_not_awaited()
    assert not flow.client.submit_calls
    assert not flow.graph.calls
    assert not flow.output_index._collection.documents


@pytest.mark.asyncio
async def test_conflicting_selectors_and_invalid_selection_fail_before_work():
    flow = extended_flow()
    for changes in ({'output_profile': 'health_report', 'output_selection': V1}, {'output_selection': 'latest'}):
        with pytest.raises(ValueError):
            await flow.service.process_document(**request(**changes))
    flow.downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_versions_coexist_without_changing_legacy_or_base_report():
    flow = extended_flow()
    newer = add_v2(flow)
    base = await flow.service.process_document(**request())
    identity = {'document_id': base.document_id, 'user_id': 'user-1'}
    first, second = await asyncio.gather(
        flow.service.process_document(**identity, output_selection=V1),
        flow.service.process_document(**identity, output_selection=V2),
    )
    legacy = await flow.service.process_document(**identity, output_profile='report_source')
    record = await flow.service.process_document(**identity)
    assert first.extraction_id == legacy.extraction_id != second.extraction_id
    assert first.output_selection == legacy.output_selection == V1
    assert second.output_selection == V2
    assert second.structured_data['sourceSummary'] == 'new projection'
    assert record.report == first.report == base.report
    assert flow.extractor.await_count == newer.await_count == 1
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_contract_revision_separates_same_implementation_label():
    flow = extended_flow()
    newer = AsyncMock(return_value={**deepcopy(flow.extractor.return_value), 'sourceSummary': 'contract two'})
    selection = OutputSelection('report_source', '2', V1.implementation_revision)
    flow.service.output_profiles.register_report_source(
        newer, version=selection.implementation_revision, contract_version='2', legacy=False
    )
    old = await flow.service.process_document(**request(output_selection=V1))
    new = await flow.service.process_document(user_id='user-1', document_id=old.document_id, output_selection=selection)
    assert old.extraction_id != new.extraction_id
    assert old.structured_data.get('sourceSummary') != new.structured_data['sourceSummary']


@pytest.mark.asyncio
@pytest.mark.parametrize('cache_first', [False, True])
async def test_interpreter_upgrade_uses_persisted_input_version_on_cache_miss_and_hit(cache_first):
    flow = extended_flow()
    base = await flow.service.process_document(**request())
    version = flow.interpreter.VERSION
    identity = {'document_id': base.document_id, 'user_id': 'user-1', 'output_selection': V1}
    previous = await flow.service.process_document(**identity) if cache_first else None
    flow.interpreter.VERSION = 'test-upgraded-interpreter'
    flow.service._document_outputs = DocumentOutputCoordinator(
        flow.service, flow.output_index, flow.service.output_profiles
    )
    result = await flow.service.process_document(**identity)
    assert result.input_snapshot.interpretation_revision == version
    assert result.input_snapshot.validation_revision == version
    stored = flow.output_index._collection.documents[result.extraction_id]
    assert stored['inputSnapshots'][0]['snapshot']['interpretation_revision'] == version
    assert stored['interpretationExecutionVersion'] is None
    assert flow.extractor.call_args.args[0][0].input_snapshot == result.input_snapshot
    assert flow.extractor.await_count == len(flow.graph.calls) == 1
    if previous is not None:
        assert result.extraction_id == previous.extraction_id
        assert result.source_revision == previous.source_revision


@pytest.mark.asyncio
async def test_legacy_cache_is_adopted_without_new_source_revision_or_job():
    flow = extended_flow()
    result = await flow.service.process_document(**request(output_selection=V1))
    rows = flow.output_index._collection.documents
    current = rows.pop(result.extraction_id)
    legacy_key = current['publicationBaseKey']
    # Recreate the exact old index shape: no contract or input-snapshot metadata.
    legacy = {
        key: deepcopy(current[key])
        for key in (
            'userId',
            'subjectId',
            'documents',
            'sourceRevisions',
            'outputProfile',
            'extractorVersion',
            'reportTypeHint',
            'byteSize',
            'status',
            'payloadRefs',
            'leaseExpiresAt',
            'createdAt',
            'updatedAt',
            'interpretationRound',
            'outputResultRound',
            'sourceRevision',
            'publicationKey',
        )
    }
    legacy.update(
        _id=f'extraction_{legacy_key}', extractionKey=legacy_key, interpretationVersion=flow.interpreter.VERSION
    )
    rows[legacy['_id']] = legacy
    jobs = len(flow.jobs.jobs)
    again = await flow.service.process_document(user_id='user-1', document_id=result.document_id, output_selection=V1)
    assert again.extraction_id == legacy['_id']
    assert again.source_revision == result.source_revision
    assert len(rows) == 1
    assert len(flow.jobs.jobs) == jobs
    assert flow.extractor.await_count == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_missing_historical_version_remains_unknown_not_current():
    flow = extended_flow()
    base = await flow.service.process_document(**request())
    stored = flow.index_collection.documents[base.document_id]
    stored.pop('interpretationVersion')
    stored.pop('interpretationValidatedVersion')
    result = await flow.service.process_document(user_id='user-1', document_id=base.document_id, output_selection=V1)
    assert result.input_snapshot.interpretation_revision is None
    assert result.input_snapshot.validation_revision is None
    cached = flow.output_index._collection.documents[result.extraction_id]
    assert cached['inputSnapshots'][0]['snapshot']['interpretation_revision'] is None
    assert len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_unavailable_old_version_does_not_resume_with_new_extractor():
    flow = extended_flow()
    original = flow.source.save_revision
    flow.source.save_revision = AsyncMock(side_effect=RuntimeError('publication unavailable'))
    with pytest.raises(RuntimeError):
        await flow.service.process_document(**request(output_selection=V1))
    flow.source.save_revision = original
    registry = DocumentOutputProfiles()
    newer = AsyncMock(return_value={})
    registry.register_report_source(newer, version=V2.implementation_revision, contract_version='2')
    flow.service.output_profiles = registry
    flow.service._document_outputs = DocumentOutputCoordinator(flow.service, flow.output_index, registry)
    with pytest.raises(UnsupportedOutputVersionError):
        await flow.service.process_document(**request(output_selection=V1))
    newer.assert_not_awaited()
    assert flow.extractor.await_count == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_recovery_reuses_fixed_output_when_another_revision_is_registered():
    flow = extended_flow()
    original = flow.source.save_revision
    flow.source.save_revision = AsyncMock(side_effect=RuntimeError('publication unavailable'))
    with pytest.raises(RuntimeError):
        await flow.service.process_document(**request(output_selection=V1))
    newer = add_v2(flow)
    flow.source.save_revision = original
    result = await flow.service.process_document(**request(output_selection=V1))
    assert result.output_selection == V1
    assert result.structured_data['metrics']
    newer.assert_not_awaited()
    assert flow.extractor.await_count == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_execution_version_change_before_resume_is_rejected_without_model_call():
    flow = extended_flow()
    base = await flow.service.process_document(**request())

    def spawn(coroutine):
        flow.interpreter.VERSION = 'unexpected-new-implementation'
        return asyncio.create_task(coroutine)

    flow.service._document_outputs.task_spawner = spawn
    with pytest.raises(UnsupportedOutputVersionError):
        await flow.service.process_document(user_id='user-1', document_id=base.document_id, report_type_hint='lab')
    assert len(flow.graph.calls) == 1
    record = next(iter(flow.output_index._collection.documents.values()))
    assert record['status'] == 'failed'
    assert record['retryable'] is False


@pytest.mark.asyncio
async def test_group_selection_preserves_order_duplicates_and_version_partition():
    flow = extended_flow()
    newer = add_v2(flow)
    first = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7 first', file_name='first.pdf')
    second = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7 second', file_name='second.pdf')
    files = [{'documentId': first.document_id}, {'documentId': second.document_id}, {'documentId': first.document_id}]
    old = await flow.service.process_documents(files, user_id='user-1', output_selection=V1)
    new = await flow.service.process_documents(files, user_id='user-1', output_selection=V2)
    again = await flow.service.process_documents(files, user_id='user-1')
    assert old.extraction_id == again.extraction_id != new.extraction_id
    assert [item['fileIndex'] for item in new.documents] == [0, 1, 2]
    assert [item['documentId'] for item in new.documents] == [first.document_id, second.document_id, first.document_id]
    assert [item.document_id for item in newer.call_args.args[0]] == [first.document_id, second.document_id]
    assert flow.extractor.await_count == newer.await_count == 1
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == 2


@pytest.mark.asyncio
async def test_changed_snapshot_reference_does_not_reuse_old_cache():
    from dataclasses import replace

    flow = extended_flow()
    base = await flow.service.process_document(**request())
    coordinator = flow.service._document_outputs
    old = await coordinator.process([base], user_id='user-1', subject_id='subject-self', output_selection=V1)
    changed = replace(
        base, input_snapshot=replace(base.input_snapshot, interpretation_artifact_ref='different-artifact')
    )
    new = await coordinator.process([changed], user_id='user-1', subject_id='subject-self', output_selection=V1)
    assert new.extraction_id != old.extraction_id
    assert flow.extractor.await_count == 2
    # A metadata-only cache migration does not create another logical publication.
    assert new.source_revision == old.source_revision
    assert len(flow.jobs.jobs) == 2


async def legacy_cache_with_deployment_version(flow, result, version):
    coordinator = flow.service._document_outputs
    rows = flow.output_index._collection.documents
    current = rows.pop(result.extraction_id)
    identity = {
        field: current[field]
        for field in ('documents', 'sourceRevisions', 'outputProfile', 'extractorVersion', 'reportTypeHint')
    }
    identity['interpretationVersion'] = version
    key = coordinator._key(identity)
    source = await coordinator._payload(current, 'source_result')
    saved = await flow.source.save_revision(
        canonical_report_id=result.canonical_report_id,
        user_id='user-1',
        subject_id='subject-self',
        source_key=key,
        result=source,
    )
    legacy = {
        field: deepcopy(current[field])
        for field in (
            'userId',
            'subjectId',
            'byteSize',
            'status',
            'payloadRefs',
            'leaseExpiresAt',
            'createdAt',
            'updatedAt',
            'interpretationRound',
            'outputResultRound',
        )
    }
    legacy.update(
        identity,
        _id=f'extraction_{key}',
        extractionKey=key,
        publicationKey=key,
        sourceRevision=saved.record.source_revision,
    )
    rows[legacy['_id']] = legacy
    return legacy


@pytest.mark.asyncio
async def test_old_cache_mislabeled_with_deployment_version_keeps_publication_identity():
    flow = extended_flow()
    result = await flow.service.process_document(**request(output_selection=V1))
    legacy = await legacy_cache_with_deployment_version(flow, result, 'different-deployment-version')
    jobs = len(flow.jobs.jobs)
    flow.interpreter.VERSION = 'another-deployment'
    read = await flow.service.process_document(user_id='user-1', document_id=result.document_id, output_selection=V1)
    assert read.extraction_id == legacy['_id']
    assert read.source_revision == legacy['sourceRevision']
    assert len(flow.jobs.jobs) == jobs
    assert flow.extractor.await_count == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_mixed_input_versions_reuse_old_group_cache_without_republication():
    flow = extended_flow()
    first = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7 first', file_name='first.pdf')
    flow.interpreter.VERSION = 'second-input-version'
    second = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7 second', file_name='second.pdf')
    assert first.input_snapshot.interpretation_revision != second.input_snapshot.interpretation_revision
    files = [{'documentId': first.document_id}, {'documentId': second.document_id}]
    result = await flow.service.process_documents(files, user_id='user-1', output_selection=V1)
    legacy = await legacy_cache_with_deployment_version(flow, result, 'second-input-version')
    jobs = len(flow.jobs.jobs)
    flow.interpreter.VERSION = 'third-deployment-version'
    read = await flow.service.process_documents(files, user_id='user-1', output_selection=V1)
    assert read.extraction_id == legacy['_id']
    assert read.source_revision == legacy['sourceRevision']
    assert len(flow.jobs.jobs) == jobs
    assert flow.extractor.await_count == 1
    assert len(flow.graph.calls) == 2


@pytest.mark.asyncio
async def test_ambiguous_legacy_results_require_reconciliation_instead_of_latest():
    flow = extended_flow()
    result = await flow.service.process_document(**request(output_selection=V1))
    legacy = await legacy_cache_with_deployment_version(flow, result, 'old-deployment-version')
    duplicate = {
        **deepcopy(legacy),
        '_id': 'another-extraction',
        'extractionKey': 'another-key',
        'interpretationVersion': 'newer-deployment',
    }
    flow.output_index._collection.documents[duplicate['_id']] = duplicate
    jobs = len(flow.jobs.jobs)
    with pytest.raises(OutputCacheConflictError):
        await flow.service.process_document(user_id='user-1', document_id=result.document_id, output_selection=V1)
    assert len(flow.jobs.jobs) == jobs
    assert flow.extractor.await_count == 1


@pytest.mark.asyncio
async def test_semantic_legacy_lookup_preserves_scope_order_source_and_hint_boundaries():
    flow = extended_flow()
    identity = {
        'documents': ['a', 'b'],
        'sourceRevisions': [1, 2],
        'outputProfile': 'report_source',
        'extractorVersion': V1.implementation_revision,
        'reportTypeHint': None,
    }
    matching = {**identity, '_id': 'matching', 'userId': 'user-1', 'subjectId': 'subject-self'}
    rows = flow.output_index._collection.documents
    rows['matching'] = matching
    for index, changes in enumerate(
        (
            {'userId': 'another-user'},
            {'subjectId': 'another-subject'},
            {'documents': ['b', 'a']},
            {'sourceRevisions': [2, 1]},
            {'reportTypeHint': 'lab'},
            {'extractorVersion': 'another-extractor'},
            {'cacheIdentityVersion': 2},
            {'contractVersion': '2'},
            {'outputProfile': 'another-profile'},
        )
    ):
        rows[str(index)] = {**deepcopy(matching), **changes, '_id': str(index)}
    found = await flow.output_index.find_legacy_outputs(user_id='user-1', subject_id='subject-self', identity=identity)
    assert [item['_id'] for item in found] == ['matching']


@pytest.mark.parametrize(
    'selection',
    [
        OutputSelection('health_report', '1', 'analysis-health-report-v12'),
        OutputSelection('health_report', '2', 'analysis-health-report-v13'),
    ],
)
@pytest.mark.asyncio
async def test_report_extraction_has_no_caller_execution_version_choice(selection):
    flow = extended_flow()
    with pytest.raises(ValueError):
        await flow.service.process_document(**request(output_selection=selection))
    flow.downloader.download.assert_not_awaited()
    assert not flow.client.submit_calls and not flow.graph.calls
    assert not flow.index_collection.documents and not flow.output_index._collection.documents
