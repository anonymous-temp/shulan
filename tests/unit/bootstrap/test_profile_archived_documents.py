"""Default root with actual document/profile providers and offline boundaries."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from adapters.profile_document_archives import ProfileDocumentArchiveAdapter
from bootstrap.factory import build_container
from health_profile.service import HealthProfileService
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.services.chat.test_chat_main_agent import manager_for


@pytest.mark.asyncio
async def test_default_root_exposes_saved_pending_and_applied_report_without_duplicate_processing(monkeypatch):
    from datetime import UTC
    from datetime import datetime
    from unittest.mock import AsyncMock

    from pymongo import ReadPreference
    from pymongo.write_concern import WriteConcern

    from bootstrap.document_sources import DocumentSourceResources
    from bootstrap.document_sources import build_current_document_source_graph
    from modules.health_profile.application import create_application_profile_public_contracts
    from tests.unit._support.current_document import bson_collection
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import actual_analysis_composition

    flow = await actual_analysis_composition(monkeypatch, [model_report()])
    flow.upstream.source.get_owned = AsyncMock(return_value=None)
    documents = flow.upstream.index_collection
    documents.with_options = lambda **options: documents
    documents.name = 'documents'
    from collections import defaultdict

    documents.database = defaultdict(bson_collection)
    from tests.unit._support.document_profile_composition.analysis.document_formal_source_store import Collection

    deliveries = Collection()
    deliveries.with_options = lambda **options: deliveries
    documents.database['documents_analysis_current_deliveries'] = deliveries
    for collection in (flow.ledger.sources, flow.ledger.intents):
        collection.read_preference = ReadPreference.PRIMARY
        collection.write_concern = WriteConcern(w='majority')
    resources = DocumentSourceResources(
        documents, flow.traces, flow.trace_chunks, bson_collection(), bson_collection(), flow.ledger
    )
    for collection in flow.cols.values():
        collection.with_options = lambda collection=collection, **options: collection
    profile = HealthProfileService(flow.repository)
    flow.analysis._health_profile_service = profile
    manager, _, _, _, _ = manager_for()
    dependencies = _dependencies()
    for key in ('health_profile_service', 'document_output_profiles', 'health_profile_ingestion_worker'):
        dependencies.pop(key)

    now = [datetime.now(UTC)]

    def current_graph(**kwargs):
        result = build_current_document_source_graph(**kwargs)
        result.current.recovery.clock = lambda: now[0]
        return result

    def profile_contracts(**kwargs):
        profile_contracts.value = create_application_profile_public_contracts(
            **kwargs, basic_info_collection=flow.cols['basic_info']
        )
        return profile_contracts.value

    with (
        patch('bootstrap.factory._create_health_profile_service', return_value=profile),
        patch('bootstrap.document_sources.create_application_document_source_resources', return_value=resources),
        patch('bootstrap.factory.create_application_health_report_interpreter'),
        patch('bootstrap.factory.HealthReportProcessingService', return_value=flow.analysis),
        patch('bootstrap.document_sources.build_current_document_source_graph', current_graph),
        patch('bootstrap.factory._create_health_profile_ingestion_worker', return_value=object()),
        patch('modules.health_profile.application.create_application_profile_public_contracts', profile_contracts),
    ):
        container = build_container(
            manager._agent_runtime,
            **dependencies,
            report_pipeline=object(),
            report_followup_service=object(),
            gut_barrier_service=object(),
            questionnaire_service=object(),
            qa_manager_type=_RecordingManager,
            chat_manager_type=_RecordingManager,
        )

    try:
        processing = container.analysis_service_factory()
        result = await processing.process(
            user_id=flow.scope.user_id, file_id='https://example.test/archive.pdf', report_id='alias'
        )
        scope = {'user_id': flow.scope.user_id, 'subject_id': flow.scope.subject_id}
        complete = await profile.list_document_observations(**scope, view='complete')
        assert not complete['items'] and not complete['documents']
        assert not (await profile.list_document_observations(**scope))['items']
        assert not flow.cols['facts'].documents
        workers = dict(container.source_workers)
        assert 'health-analysis-basic-info' in workers
        assert 'document-formal' not in workers
        processing.publication.delivery.clock = lambda: now[0].timestamp()
        for _ in range(4):
            await workers['analysis-delivery'].recover_pending()
            await profile_contracts.value.worker.recover_pending()
            now[0] += timedelta(seconds=60)
            await workers['document-outbox'].recover_pending()
        assert len(flow.cols['publications'].documents) > 0
        assert len(flow.ledger.sources.documents) == 1
        state = next(iter(flow.ledger.sources.documents.values()))
        assert state['appliedRevision'] == 1, [
            (row.get('recordKind'), row.get('status'), row.get('errorCode'))
            for row in flow.cols['publications'].documents
        ]
        complete = await profile.list_document_observations(**scope, view='complete')
        assert complete['documents'][0]['sourceStatus'] == 'active'
        assert result['reportId'] == 'alias'
        assert len(flow.gateway.complete_requests) == len(flow.upstream.client.submit_calls) == 1
    finally:
        await container.resources.aclose()


@pytest.mark.asyncio
async def test_storage_adapter_translates_lifecycle_change_and_preserves_scope():
    from unittest.mock import AsyncMock

    from health_profile.document_archive_contracts import ProfileDocumentArchiveChangedError
    from modules.analysis.archived_extractions import DocumentArchiveChangedError
    from modules.analysis.snapshots import DocumentScope

    provider = AsyncMock()
    provider.read_fixed.side_effect = DocumentArchiveChangedError('changed')
    adapter = ProfileDocumentArchiveAdapter(provider)
    with pytest.raises(ProfileDocumentArchiveChangedError):
        await adapter.read_fixed(user_id='u', subject_id='s', extraction_id='fixed', expected_lifecycle_token='token')
    assert provider.read_fixed.call_args.kwargs['scope'] == DocumentScope('u', 's')


@pytest.mark.asyncio
@pytest.mark.parametrize('view', ['standard', 'complete'])
@pytest.mark.parametrize('action', ['retire', 'withdraw'])
@pytest.mark.parametrize('missing_secondary_index', [False, True])
async def test_actual_secondary_file_profile_pages_keep_scope_and_drop_retired_combined_contribution(
    view, action, missing_secondary_index
):
    from dataclasses import replace

    from health_profile.service import HealthProfileService
    from tests.unit.services.analysis.test_archived_extractions import combined_source
    from tests.unit.services.analysis.test_document_formal_source import apply

    current, secondary, reader, command, published = await combined_source()
    selected = secondary.material.payload['documentId']
    if missing_secondary_index:
        del current.env.flow.index_collection.documents[selected]
    profile = HealthProfileService(
        current.profile.repository,
        clock=lambda: current.profile.now[0],
        document_archive_reader=ProfileDocumentArchiveAdapter(reader),
    )
    scope = {'user_id': current.scope.user_id, 'subject_id': current.scope.subject_id}
    original_calls = (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls))

    async def read_pages():
        collected, cursor = [], None
        for _ in range(30):
            page = await profile.list_document_observations(
                **scope, document_id=selected, view=view, limit=1, cursor=cursor
            )
            assert len(page['items']) <= 1
            collected.extend(page['items'])
            cursor = page['nextCursor']
            if cursor is None:
                return collected
        raise AssertionError('actual filtered Profile pagination did not terminate')

    before = await read_pages()
    assert before and all(selected in row['documentIds'] for row in before)
    assert len({row['observationId'] for row in before}) == len(before)
    related = [row for row in before if row['canonicalReportId'] == command.source_id]
    assert related and all('/reportSource/' in row['sourcePointer'] for row in related)
    own = [row for row in before if row['canonicalReportId'] == secondary.material.source_id]
    assert bool(own) is not missing_secondary_index
    if action == 'retire':
        await current.runtime.commands.execute(
            replace(
                command,
                request_id='profile-retire-combined',
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
            request_id='profile-withdraw-combined',
            expected_prior_revision=published['publicationRevision'],
        )
    await apply(current)
    after = await read_pages()
    assert bool(after) is not missing_secondary_index
    assert all(row['canonicalReportId'] == secondary.material.source_id for row in after)
    assert (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls)) == original_calls


@pytest.mark.asyncio
async def test_actual_missing_archive_documents_page_without_fabricating_or_dereferencing_extraction_ids():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from health_profile.service import HealthProfileService
    from modules.analysis.understanding import UNDERSTANDING_SELECTION
    from tests.unit.services.analysis.test_archived_extractions import reader as archive_reader
    from tests.unit.services.analysis.test_document_formal_source import flow

    current = await flow()
    await current.env.api.prepare_bundle(
        scope=current.scope,
        selection=UNDERSTANDING_SELECTION,
        content=b'%PDF-1.7 second missing archive',
        file_name='missing.pdf',
    )
    for document in current.env.flow.index_collection.documents.values():
        document.pop('archivedRounds', None)
        document['payloadRefs'] = {}
    reader = archive_reader(
        SimpleNamespace(flow=current.env.flow, api=current.env.api.understanding), managed=current.runtime
    )
    reader.lifecycle = current.runtime.lifecycle
    reader.read_fixed = AsyncMock(side_effect=AssertionError('missing extraction must not be dereferenced'))
    profile = HealthProfileService(
        current.profile.repository,
        clock=lambda: current.profile.now[0],
        document_archive_reader=ProfileDocumentArchiveAdapter(reader),
    )
    scope = {'user_id': current.scope.user_id, 'subject_id': current.scope.subject_id}
    documents, cursor = [], None
    original_calls = (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls))
    for _ in range(5):
        page = await profile.list_document_observations(**scope, view='complete', limit=1, cursor=cursor)
        assert page['items'] == [] and len(page['documents']) <= 1
        documents.extend(page['documents'])
        cursor = page['nextCursor']
        if cursor is None:
            break
    assert cursor is None and len(documents) == 2
    assert len({row['documentId'] for row in documents}) == 2
    assert all(row['extractionId'] is None and row['archiveStatus'] == 'unavailable' for row in documents)
    assert (await profile.list_document_observations(**scope))['items'] == []
    reader.read_fixed.assert_not_called()
    assert (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls)) == original_calls


@pytest.mark.asyncio
async def test_explicit_archive_reader_binds_with_an_explicit_historical_processing_factory():
    from health_profile.repository import MongoHealthProfileRepository
    from health_profile.service import HealthProfileService
    from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection

    manager, _, _, _, _ = manager_for()
    collections = {name: FakeCollection() for name in ('subjects', 'facts', 'events', 'views', 'jobs', 'publications')}
    profile = HealthProfileService(MongoHealthProfileRepository(**collections))
    adapter = ProfileDocumentArchiveAdapter(None)
    dependencies = _dependencies()
    dependencies['health_profile_service'] = profile
    container = build_container(
        manager._agent_runtime,
        **dependencies,
        health_report_processing_service_factory=lambda: object(),
        document_archive_reader=adapter,
        record_document_port=object(),
        report_document_source_factory=object(),
        report_pipeline=object(),
        report_followup_service=object(),
        gut_barrier_service=object(),
        questionnaire_service=object(),
        qa_manager_type=_RecordingManager,
        chat_manager_type=_RecordingManager,
    )
    try:
        assert container.health_profile_service is profile
        assert profile._document_archive_reader is adapter
        assert not container.startup_initializers
    finally:
        await container.resources.aclose()


@pytest.mark.asyncio
async def test_new_archive_outage_preserves_active_standard_rows_and_recovers_same_complete_selection(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from health_profile.service import HealthProfileService
    from modules.analysis.application.document_reuse import _CheckpointTrace
    from tests.unit.services.analysis.test_archived_extractions import combined_source

    current, _, reader, _, _ = await combined_source()
    document_id = current.bundle.material.payload['documentId']
    profile = HealthProfileService(
        current.profile.repository,
        clock=lambda: current.profile.now[0],
        document_archive_reader=ProfileDocumentArchiveAdapter(reader),
    )
    query = {'user_id': current.scope.user_id, 'subject_id': current.scope.subject_id, 'document_id': document_id}
    standard = await profile.list_document_observations(**query)
    assert standard['items']
    original_calls = (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls))
    lifecycle = await reader.read_source_lifecycle(
        scope=current.scope, canonical_report_id=current.bundle.material.source_id
    )
    traces = reader.traces
    next_trace = 'profile-new-pending-archive'
    result = deepcopy(current.bundle.understanding.payload['interpretation'])
    result['observations'][0]['resultValue'] = '477'
    await traces.create({'_id': next_trace, **current.scope.as_dict()})
    await traces.write_payload(next_trace, current.scope.user_id, 'interpretation', result)
    document = current.env.flow.index_collection.documents[document_id]
    reuse = current.env.flow.service._document_reuse
    document.update(
        interpretationRound=2,
        interpretationResultRound=2,
        status='running',
        leaseToken='new-archive-owner',
        leaseExpiresAt=reuse.now() + timedelta(minutes=2),
    )
    document['payloadRefs'] = {**document['payloadRefs'], 'interpretation': next_trace}
    checkpoint = _CheckpointTrace(reuse, document, 'new-archive-owner', SimpleNamespace(trace_id=next_trace))
    await checkpoint.archive_round(result)
    await checkpoint.save(status='completed', leaseToken=None, leaseExpiresAt=None)
    complete = await profile.list_document_observations(**query, view='complete')
    extraction_id = complete['documents'][0]['extractionId']
    assert complete['documents'][0]['sourceStatus'] == 'pending'
    assert any(row['observation'].get('resultValue') == '477' for row in complete['items'])
    assert await profile.list_document_observations(**query) == standard
    read_payload = traces.read_payload
    broken = True

    async def interrupted(trace_id, user_id, subject_id, name):
        if broken and trace_id == next_trace and name == 'interpretation':
            raise RuntimeError('temporary new archive read failure')
        return await read_payload(trace_id, user_id, subject_id, name)

    monkeypatch.setattr(traces, 'read_payload', interrupted)
    assert await profile.list_document_observations(**query) == standard
    unavailable = await profile.list_document_observations(**query, view='complete')
    assert unavailable['items'] == [] and unavailable['status'] == 'unavailable'
    assert unavailable['documents'][0]['extractionId'] == extraction_id
    assert unavailable['documents'][0]['archiveStatus'] == 'unavailable'
    assert (
        await reader.read_source_lifecycle(scope=current.scope, canonical_report_id=current.bundle.material.source_id)
        == lifecycle
    )
    broken = False
    recovered = await profile.list_document_observations(**query, view='complete')
    assert recovered == complete
    assert recovered['documents'][0]['extractionId'] == extraction_id
    assert (len(current.env.flow.graph.calls), len(current.env.flow.client.submit_calls)) == original_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('filter_primary', [False, True])
async def test_actual_combined_complete_rows_preserve_each_contribution_file_ownership(filter_primary):
    from health_profile.service import HealthProfileService
    from tests.unit.services.analysis.test_archived_extractions import combined_source

    current, secondary, reader, _, _ = await combined_source()
    primary_id = current.bundle.material.payload['documentId']
    secondary_id = secondary.material.payload['documentId']
    profile = HealthProfileService(
        current.profile.repository,
        clock=lambda: current.profile.now[0],
        document_archive_reader=ProfileDocumentArchiveAdapter(reader),
    )
    complete = await profile.list_document_observations(
        user_id=current.scope.user_id,
        subject_id=current.scope.subject_id,
        document_id=primary_id if filter_primary else None,
        view='complete',
    )
    rows = [row for row in complete['items'] if row['canonicalReportId'] == current.bundle.material.source_id]
    originals = [row for row in rows if row['sourcePointer'].startswith('/observations/')]
    supplements = [row for row in rows if '/reportSource/' in row['sourcePointer']]
    assert originals and supplements
    assert all(row['documentId'] == primary_id and row['documentIds'] == [primary_id] for row in originals)
    assert all(row['documentId'] is None and row['documentIds'] == [primary_id, secondary_id] for row in supplements)


@pytest.mark.asyncio
async def test_actual_other_subject_reads_routed_original_observations_without_self_leak(monkeypatch):
    from adapters.analysis.archived_extractions import create_document_archived_extraction_reader
    from health_profile.basic_info_contracts import BasicInfoValuesV1
    from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import actual_analysis_composition
    from tests.unit._support.current_document_profile import settle

    flow = await actual_analysis_composition(monkeypatch, [model_report()])
    confirmation = await flow.profile.basic_info.register(
        flow.scope.user_id,
        'known-self',
        values=BasicInfoValuesV1(name='synthetic-owner', gender='male', age=50),
        confirmed_at=flow.now[0],
        subject_id=flow.scope.subject_id,
    )
    await flow.profile.basic_info.apply(flow.scope.user_id, confirmation.command_id)
    monkeypatch.setattr(
        flow.upstream.index_collection, 'with_options', lambda **options: flow.upstream.index_collection, raising=False
    )
    prepared = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/routed-custody.pdf', file_type='pdf'
    )
    await flow.runtime.consumption.consume_prepared(
        scope=flow.scope,
        prepared=flow.prepared.input(prepared),
        request_id='route-through-profile',
        lifecycle_epoch=flow.epoch,
    )
    await settle(flow)
    subjects = await flow.repository.list_subjects(flow.scope.user_id)
    (other,) = [subject for subject in subjects if subject.kind == 'other']
    reader = create_document_archived_extraction_reader(
        processing=flow.analysis,
        lifecycle=DocumentPublicationLifecycleReader(
            ledger=flow.ledger, snapshots=flow.prepared.understanding.snapshots
        ),
        current_source=flow.readers.source,
    )
    service = HealthProfileService(flow.repository)
    service.bind_document_archive_reader(
        ProfileDocumentArchiveAdapter(reader), require_profile_ownership=True, publications=flow.profile.inbox
    )

    async def complete(subject_id):
        return await service.list_document_observations(flow.scope.user_id, subject_id=subject_id, view='complete')

    source = await complete(other.subject_id)
    assert [item['observation']['itemName'] for item in source['items']] == ['ALT']
    assert [document['documentId'] for document in source['documents']] == [prepared.receipt.document_id]
    assert source['documents'][0]['title'] is None
    own = await complete(flow.scope.subject_id)
    assert not own['items'] and not own['documents']
    await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=prepared.material.source_id,
        source_revision=prepared.material.source_revision,
        request_id='withdraw-routed-provenance',
        expected_prior_revision=1,
        lifecycle_epoch=flow.epoch,
    )
    await settle(flow)
    removed = await complete(other.subject_id)
    assert not removed['items'] and not removed['documents']
