"""Root composition exercises real public adapters with isolated BSON storage and model fixtures."""

from types import SimpleNamespace

import pytest
from pymongo import ReadPreference

from adapters.analysis.document_trace import MongoDocumentTraceStore
from bootstrap.report_documents import UnavailableFixedReportDocumentFactory
from bootstrap.report_documents import build_report_document_source_factory
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.report.agent import ReportAgent
from modules.report.schemas import RemoteFileInput
from tests.unit.services.analysis.test_document_material_reader import BsonCollection
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_document_workflow import extended_flow


class Collection(BsonCollection):
    def __init__(self, database, name):
        super().__init__()
        self.database, self.name, self.options = database, name, {}

    async def find_one(self, filters, projection=None):
        row = await super().find_one(filters)
        if row is not None and projection is not None and projection.get('_id') == 0:
            row.pop('_id', None)
        return row

    def with_options(self, **options):
        self.options = options
        return self


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = Collection(self, name)
        return self.collections[name]


def environment():
    database = Database()
    flow = extended_flow()
    # Production registry starts without a fixture-specific Report extension.
    profiles = DocumentOutputProfiles()
    flow.service.output_profiles = profiles
    flow.service._document_outputs.profiles = profiles
    flow.service._document_trace_store = MongoDocumentTraceStore(database['traces'], database['chunks'])
    factory = build_report_document_source_factory(
        processing_service=flow.service,
        profile_service=flow.service._health_profile_service,
        document_collection=database['owned_documents'],
        traces=database['traces'],
        trace_chunks=database['chunks'],
        report_artifact_collection=database['owned_reports'],
    )
    return SimpleNamespace(database=database, flow=flow, factory=factory)


def test_real_composition_keeps_namespaces_ownership_and_durable_options_without_io():
    env = environment()
    assert set(env.database.collections) == {
        'traces',
        'chunks',
        'owned_documents',
        'owned_reports',
        'owned_documents_snapshots',
        'owned_documents_snapshot_chunks',
        'owned_documents_compatibility',
        'owned_documents_report_publications',
        'owned_reports_document_projections',
    }
    for name, collection in env.database.collections.items():
        assert not collection.documents and not collection.indexes
        if name not in ('owned_documents', 'owned_reports'):
            assert collection.options['read_preference'] == ReadPreference.PRIMARY
            assert collection.options['write_concern'].document == {'w': 'majority', 'j': True, 'wtimeout': 5000}
    env.flow.service._health_profile_service.ensure_self_profile.assert_not_awaited()
    assert not env.flow.graph.calls and not env.flow.jobs.jobs
    assert env.factory.publications.compatibility is env.factory.compatibility
    assert env.factory.projections.store.collection.name == 'owned_reports_document_projections'


@pytest.mark.asyncio
async def test_root_real_public_adapters_publish_legacy_once_and_reuse_original_model_and_private_cache():
    env = environment()
    reader = env.factory.create(user_id=SCOPE.user_id, session_id='session', final_report_id='final')
    source = RemoteFileInput(url='https://example.test/report.pdf', file_type='pdf')
    first = await reader.read(source, index=0)
    assert first.parsed.supplementary_hospital_data
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1
    assert len(env.flow.jobs.jobs) == 2
    cache = env.database['owned_reports_document_projections']
    assert len(cache.documents) == 1
    assert env.database['owned_documents_report_publications'].documents
    trace = env.database['traces'].documents[first.references[0]['outputTraceId']]
    assert trace['extractionId'] == first.references[0]['extractionId']
    second = await reader.read(source, index=0)
    assert second.references == first.references
    assert len(env.flow.graph.calls) == len(env.flow.jobs.jobs) - 1 == 1
    assert len(cache.documents) == 1


def test_unavailable_requested_manifest_matches_ready_versions_but_has_distinct_identity():
    ready = environment().factory.document_input_manifest()
    failed = UnavailableFixedReportDocumentFactory().document_input_manifest()
    for key in ready:
        if key == 'consumerRevision':
            assert ready[key] == failed['requestedConsumerRevision']
            assert ready[key] != failed[key]
        else:
            assert ready[key] == failed[key]


@pytest.mark.asyncio
async def test_configuration_recovery_cannot_change_a_previously_accepted_task():
    from tests.unit.report.test_durable_reports import REQUEST
    from tests.unit.report.test_durable_reports import setup

    env = setup()
    env.pipeline.document_source_factory = UnavailableFixedReportDocumentFactory()
    env.service.main_agent = ReportAgent(env.pipeline)
    task = await env.service.submit(REQUEST)
    manifest = task['executionManifest']
    env.pipeline.document_source_factory = environment().factory
    env.service.main_agent = ReportAgent(env.pipeline)
    assert (await env.service.submit(REQUEST))['executionManifest'] == manifest
    await env.service.drain_generation()
    assert env.pipeline.calls == 0
    assert env.batches.documents[task['_id']]['generationStatus'] == 'failed'
    assert all(item['body']['status'] == 'failed' for item in env.artifacts.documents.values())
