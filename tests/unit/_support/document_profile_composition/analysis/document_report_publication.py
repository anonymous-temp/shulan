"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_report_publication."""

from adapters.analysis.document_trace import MongoDocumentTraceStore
from adapters.analysis.materials import create_document_material_reader
from adapters.analysis.report_publication import create_document_report_publication_service
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.analysis.report_publication_contracts import LEGACY_REPORT_SOURCE_SELECTION
from tests.unit._support.document_profile_composition.analysis.document_compatible_publication import (
    environment as base_environment,
)
from tests.unit._support.document_profile_composition.analysis.document_material_reader import BsonCollection


def environment(*, extractor=None):
    env = base_environment()
    env.traces, env.trace_chunks, env.commands = (BsonCollection() for _ in range(3))
    env.flow.service._document_trace_store = MongoDocumentTraceStore(env.traces, env.trace_chunks)
    profiles = DocumentOutputProfiles()
    profiles.register_report_source(
        extractor or env.flow.extractor, version=LEGACY_REPORT_SOURCE_SELECTION.implementation_revision
    )
    env.flow.service.output_profiles = profiles
    env.flow.service._document_outputs.profiles = profiles
    env.materials = create_document_material_reader(
        snapshots=env.snapshots, snapshot_chunks=env.chunks, traces=env.traces, trace_chunks=env.trace_chunks
    )
    env.publisher = restart(env)
    return env


def restart(env):
    return create_document_report_publication_service(
        processing_service=env.flow.service,
        compatibility_service=env.api,
        material_reader=env.materials,
        command_records=env.commands,
    )
