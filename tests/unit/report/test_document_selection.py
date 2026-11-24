"""Report pins its own projection while other consumers retain their aliases."""

from unittest.mock import AsyncMock

import pytest

from adapters.report.document_source import REPORT_DOCUMENT_SELECTION
from adapters.report.document_source import SharedReportDocuments
from modules.analysis.application.output_profiles import OutputProfile
from modules.analysis.contracts import OutputSelection
from modules.report.document_source import extract_intrinsic_report_source
from modules.report.schemas import RemoteFileInput
from tests.unit.report.test_shared_document_source import quality_outcome
from tests.unit.report.test_shared_document_source import reader
from tests.unit.report.test_shared_document_source import report_flow


def test_report_registers_its_own_implementation_without_moving_legacy_alias():
    flow = report_flow()
    old = OutputProfile('report_source', 'report-source-old', lambda snapshots: {})
    flow.service.output_profiles.register(old)
    current = reader(flow)
    assert isinstance(current, SharedReportDocuments)
    assert flow.service.output_profiles.get('report_source') is old
    assert flow.service.output_profiles.get(REPORT_DOCUMENT_SELECTION).extractor is extract_intrinsic_report_source


@pytest.mark.asyncio
async def test_report_single_and_group_reads_pin_selection_despite_other_registered_versions():
    flow = report_flow()
    current = reader(flow)
    flow.service.output_profiles.register(OutputProfile('report_source', 'future', lambda snapshots: {}), legacy=False)
    flow.service.process_document = AsyncMock(return_value=quality_outcome(0, 'complete'))
    flow.service.process_documents = AsyncMock(return_value=quality_outcome(0, 'complete'))
    source = RemoteFileInput(url='https://example.test/current.pdf', file_type='pdf')
    await current.read(source, index=0)
    await current.combine([0])
    assert flow.service.process_document.await_args.kwargs['output_selection'] == REPORT_DOCUMENT_SELECTION
    assert flow.service.process_documents.await_args.kwargs['output_selection'] == REPORT_DOCUMENT_SELECTION


def test_conflicting_implementation_cannot_silently_use_registered_code():
    flow = report_flow()
    selection = REPORT_DOCUMENT_SELECTION
    flow.service.output_profiles.register(
        OutputProfile(
            selection.profile_name,
            selection.implementation_revision,
            lambda snapshots: {},
            True,
            selection.contract_version,
        )
    )
    assert not isinstance(reader(flow), SharedReportDocuments)


def test_first_registration_keeps_legacy_compatibility_and_exact_version_identity():
    flow = report_flow()
    reader(flow)
    assert flow.service.output_profiles.get('report_source').selection == REPORT_DOCUMENT_SELECTION
    assert REPORT_DOCUMENT_SELECTION == OutputSelection('report_source', '1', 'report-source-v16')
