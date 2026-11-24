"""Actual public Formal/Profile providers with fixed offline Document material."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.report.formal_documents import create_formal_report_document_source_factory
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.report.document_projection import ReportProjectionService
from modules.report.document_source import EXTRACTOR_VERSION
from modules.report.document_source import extract_intrinsic_report_source
from modules.report.schemas import RemoteFileInput
from tests.unit.report.test_document_projection import Cache
from tests.unit.services.analysis.test_document_complete_source import change_interpretation
from tests.unit.services.analysis.test_document_formal_source import advance
from tests.unit.services.analysis.test_document_formal_source import apply
from tests.unit.services.analysis.test_document_formal_source import flow
from tests.unit.services.analysis.test_document_formal_source import request


async def environment():
    current = await flow()
    extract = AsyncMock(side_effect=extract_intrinsic_report_source)
    profiles = DocumentOutputProfiles()
    profiles.register_report_source(extract, version=EXTRACTOR_VERSION)
    current.env.flow.service.output_profiles = profiles
    current.env.flow.service._document_outputs.profiles = profiles
    current.runtime.reports.profiles = profiles
    # Material was prepared by the real public Document command. This narrow
    # input port fixes that already selected bundle rather than downloading again.
    compatibility = SimpleNamespace(
        ensure_self_scope=AsyncMock(return_value=current.scope),
        prepare_bundle=AsyncMock(return_value=current.bundle),
    )
    private_extract = AsyncMock(side_effect=AssertionError('consumer must reuse the fixed public projection'))
    from adapters.report.fixed_documents import REPORT_FIXED_SELECTION

    projections = ReportProjectionService(Cache(), implementations={REPORT_FIXED_SELECTION: private_extract})
    current.factory = create_formal_report_document_source_factory(
        compatibility=compatibility, materials=current.env.materials, formal=current.formal, projections=projections
    )
    current.gateway = current.factory.create(
        user_id=current.scope.user_id, session_id='session', final_report_id='report'
    )
    current.extract, current.private_extract = extract, private_extract
    return current


async def read(current):
    return await current.gateway.read(RemoteFileInput(url='https://example.test/fresh.pdf', file_type='pdf'), index=0)


@pytest.mark.asyncio
async def test_real_fresh_formal_pending_renders_then_independent_recovery_applies_supplement():
    current = await environment()
    result = await read(current)
    reference = result.references[0]
    assert reference['processingStatus'] == 'completed'
    assert reference['formalSource']['status'] == 'pending'
    assert result.parsed.supplementary_hospital_data
    assert current.extract.await_count == 1
    current.private_extract.assert_not_awaited()
    command_id = reference['formalSource']['commandId']
    row = await current.formal.store.get(command_id)
    command = current.formal._command(row)
    await advance(current, command)
    await apply(current)
    completed = await advance(current, command)
    assert completed['status'] == 'completed'
    assert completed['result']['projectionRef'] == reference['formalSource']['projectionRef']
    assert completed['result']['reportPublication']['publicationRevision'] == 2
    assert current.extract.await_count == 1
    assert not current.env.flow.jobs.jobs
    # A completed business response stays frozen while source delivery advances.
    assert await read(current) == result


@pytest.mark.asyncio
async def test_real_profile_unavailability_preserves_business_result_and_fixed_source_failure():
    current = await environment()
    current.fresh.reserve_fresh_source = AsyncMock(side_effect=RuntimeError('Profile offline'))
    result = await read(current)
    assert result.parsed.supplementary_hospital_data
    assert result.references[0]['processingStatus'] == 'completed'
    assert result.references[0]['formalSource']['status'] == 'pending'
    assert not current.env.flow.collection.documents and not current.env.flow.jobs.jobs
    assert current.extract.await_count == 1
    current.private_extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_managed_partial_keeps_partial_business_state_and_does_not_replace_source():
    current = await environment()
    initial = request(current)
    await current.formal.submit(initial)
    assert (await advance(current, initial))['status'] == 'completed'
    await apply(current)
    partial = await change_interpretation(current.env, current.bundle, scope=current.scope, status='partial')
    current.factory.compatibility.prepare_bundle.return_value = partial
    before = len(current.env.ledger.intents.documents)
    result = await read(current)
    assert result.references[0]['processingStatus'] == 'partial'
    assert result.references[0]['formalSource']['status'] == 'not_published'
    assert any('SHARED_INTERPRETATION_PARTIAL' in error for error in result.parsed.source_errors)
    assert len(current.env.ledger.intents.documents) == before
    current.private_extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_legacy_uses_actual_archive_hash_and_queue_without_private_reextraction():
    current = await environment()
    await current.env.api.publish_prepared(
        scope=current.scope,
        understanding_snapshot_id=current.item.understanding_snapshot_ref,
        selection=current.item.selection,
        preparation_receipt=current.item.preparation_receipt,
    )
    result = await read(current)
    reference = result.references[0]
    assert reference['formalSource']['status'] == 'completed'
    assert reference['formalSource']['commandRevision'] == 'document-formal-source-v2'
    assert type(reference['sourceRevision']) is int and reference['sourceRevision'] > 1
    assert reference['ingestionStatus'] == 'queued' and reference['jobId']
    receipt = await current.formal.read_receipt(scope=current.scope, command_id=reference['formalSource']['commandId'])
    published = receipt['result']['reportPublication']
    assert reference['extractionId'] == published['extractionId']
    assert reference['outputTraceId'] == published['outputTraceId']
    prepared = await current.formal.read_report_projection(
        scope=current.scope, projection_ref=reference['formalSource']['projectionRef']
    )
    assert reference['formalSource']['preparationOutputTraceId'] == prepared.payload['outputTraceId']
    assert len(current.env.flow.jobs.jobs) == 2
    assert current.extract.await_count == 1
    assert (
        await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    ).mode == 'legacy'
    current.private_extract.assert_not_awaited()
