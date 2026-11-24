"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_managed_source."""

from types import SimpleNamespace

from adapters.analysis.complete_source import ProfileDocumentCompleteSourceReader
from adapters.analysis.managed_source import create_document_managed_source_runtime
from health_profile.document_source import ProfileDocumentSourceResolver
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.understanding import UNDERSTANDING_SELECTION


def item(bundle):
    return DocumentPreparedPublicationInput(
        bundle.material.snapshot_id,
        bundle.facts.snapshot_id,
        bundle.understanding.snapshot_id,
        UNDERSTANDING_SELECTION,
        bundle.receipt,
    )


def restart(flow):
    runtime = create_document_managed_source_runtime(
        compatibility=flow.env.api,
        report_publications=flow.env.publisher,
        material_reader=flow.env.materials,
        source_admission=flow.admission,
        publication_runtime=SimpleNamespace(ledger=flow.env.ledger, recovery=flow.recovery),
        profile_archive=flow.archive,
        command_records=flow.command_records,
    )
    resolver = ProfileDocumentSourceResolver(ProfileDocumentCompleteSourceReader(runtime.source_reader))
    flow.profile.inbox.document_source = resolver
    flow.profile.applier.resolvers[('document', COMPLETE_SOURCE_CONTRACT)] = resolver
    flow.profile.applier.resolvers[('document', RETRACTION_CONTRACT)] = resolver
    return runtime
