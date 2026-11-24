"""Public, I/O-free registration of Report's historical Document extension."""

from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.report.document_source import EXTRACTOR_VERSION
from modules.report.document_source import extract_intrinsic_report_source
from modules.report.ports import DocumentServiceError

REPORT_LEGACY_SOURCE_SELECTION = OutputSelection('report_source', '1', 'report-source-v16')


def configure_report_source_profiles(processor):
    """Register/verify the real v16 implementation without moving an existing alias."""
    if not processor.supports_document_reuse:
        raise DocumentServiceError('shared document reuse must be configured')
    if EXTRACTOR_VERSION != REPORT_LEGACY_SOURCE_SELECTION.implementation_revision:
        raise UnsupportedOutputVersionError('the historical Report extractor implementation is unavailable')
    profiles = processor.output_profiles
    try:
        profile = profiles.get(REPORT_LEGACY_SOURCE_SELECTION)
    except UnsupportedOutputVersionError:
        try:
            profiles.get('report_source')
        except UnsupportedOutputVersionError:
            legacy = True
        else:
            legacy = False
        profiles.register_report_source(
            extract_intrinsic_report_source,
            version=EXTRACTOR_VERSION,
            needs_bytes=True,
            contract_version=REPORT_LEGACY_SOURCE_SELECTION.contract_version,
            legacy=legacy,
        )
    else:
        if profile.extractor is not extract_intrinsic_report_source or not profile.needs_bytes:
            raise DocumentServiceError('report_source extractor registration conflict')
