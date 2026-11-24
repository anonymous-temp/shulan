"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_document_complete_source."""

from modules.analysis.complete_source import DocumentCompleteSourceService


def service(env, admission=None):
    return DocumentCompleteSourceService(
        snapshots=env.api.understanding.snapshots,
        understanding=env.api.understanding,
        report_publications=env.publisher,
        admission=admission,
    )
