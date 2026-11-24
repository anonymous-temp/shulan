"""Public-contract fakes for the independently versioned Report document consumer."""

import asyncio
from dataclasses import asdict
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.report.fixed_documents import REPORT_FIXED_SELECTION
from adapters.report.fixed_documents import create_fixed_report_document_source_factory
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.service import HealthReportUpstreamError
from modules.analysis.compatibility import DocumentLegacyPublicationReceipt
from modules.analysis.report_publication_contracts import DocumentReportPublicationManifest
from modules.analysis.report_publication_contracts import DocumentReportPublicationReceipt
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.report.document_projection import ReportProjectionService
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.ports import DocumentServiceError
from modules.report.schemas import RemoteFileInput
from tests.unit.report.test_document_projection import Cache
from tests.unit.report.test_document_projection import frozen_input

SOURCE = RemoteFileInput(url='https://example.test/report.pdf', file_type='pdf')


class Publications:
    """Public outputTraceId is explicit and never fabricated by the consumer."""

    def __init__(self, bundle):
        self.bundle, self.calls, self.snapshots = bundle, [], {}
        self.after_write = False
        self.omit_trace = False

    async def publish_report_source(self, *, scope, inputs, selection):
        manifest = DocumentReportPublicationManifest(scope, inputs, selection)
        self.calls.append(manifest)
        base = tuple(
            DocumentLegacyPublicationReceipt(
                item.preparation_receipt, self.bundle.material.source_id, 1, 'pending', 'base-job'
            )
            for item in inputs
        )
        payload = {
            'selection': asdict(selection),
            'extractionId': 'real-legacy-extraction',
            'legacySourceRevision': 77,
            'processingStatus': 'complete',
            'projection': {'subject': {'name': 'LEGACY MUST NOT RENDER'}},
        }
        if not self.omit_trace:
            payload['outputTraceId'] = 'real-legacy-output-trace'
        projection = DocumentSnapshot(
            scope, 'document.legacy-report-source.v1', self.bundle.material.source_id, 1, canonical_json(payload)
        )
        self.snapshots[projection.snapshot_id] = projection
        receipt = DocumentReportPublicationReceipt(
            manifest, projection.snapshot_id, base, 77, 'pending', 'supplement-job', 'complete'
        )
        if self.after_write:
            self.after_write = False
            raise RuntimeError('publication ACK lost')
        return receipt

    async def read_projection(self, *, scope, projection_ref, selection):
        return self.snapshots[projection_ref]


async def environment():
    _, bundle, source = await frozen_input()
    compatibility = SimpleNamespace(
        ensure_self_scope=AsyncMock(return_value=bundle.material.scope), prepare_bundle=AsyncMock(return_value=bundle)
    )
    materials = SimpleNamespace(
        read_original=AsyncMock(return_value=source.content), capture_failed_material=AsyncMock(return_value=None)
    )
    extractor = AsyncMock(
        return_value={
            'subject': {'name': 'REPORT OWN CONTENT'},
            'supplementaryHospitalData': [
                {'dataType': 'INSPECTION', 'itemName': 'synthetic observation', 'testResultValue': '5'},
            ],
        }
    )
    projections = ReportProjectionService(Cache(), implementations={REPORT_FIXED_SELECTION: extractor})
    publications = Publications(bundle)
    factory = create_fixed_report_document_source_factory(
        compatibility=compatibility,
        materials=materials,
        publications=publications,
        projections=projections,
        policy=ReportFallbackPolicy(retry_backoff_seconds=(0,)),
    )
    gateway = factory.create(user_id=bundle.material.scope.user_id, session_id='session', final_report_id='final')
    return SimpleNamespace(
        bundle=bundle,
        source=source,
        compatibility=compatibility,
        materials=materials,
        extractor=extractor,
        projections=projections,
        publications=publications,
        factory=factory,
        gateway=gateway,
    )


@pytest.mark.asyncio
async def test_business_content_uses_private_report_projection_and_publication_only_supplies_actual_references():
    env = await environment()
    result = await env.gateway.read(SOURCE, index=0)
    assert result.parsed.subject.name == 'REPORT OWN CONTENT'
    assert 'LEGACY MUST NOT RENDER' not in result.parsed.model_dump_json()
    reference = result.references[0]
    assert reference['extractionId'] == 'real-legacy-extraction'
    assert reference['outputTraceId'] == 'real-legacy-output-trace'
    assert reference['sourceRevision'] == 77 and reference['jobId'] == 'supplement-job'
    assert reference['traceId'] == env.bundle.receipt.trace_id
    kwargs = env.compatibility.prepare_bundle.call_args.kwargs
    assert kwargs == {
        'scope': env.bundle.material.scope,
        'selection': UNDERSTANDING_SELECTION,
        'require_text': True,
        'file_id': SOURCE.url,
        'file_type': 'pdf',
        'source_type': 'report',
        'source_id': 'final',
        'session_id': 'session',
        'file_index': 0,
    }
    env.extractor.assert_awaited_once()


@pytest.mark.asyncio
async def test_lost_publication_ack_reuses_original_preparation_and_private_cache():
    env = await environment()
    env.publications.after_write = True
    with pytest.raises(RuntimeError, match='ACK lost'):
        await env.gateway.read(SOURCE, index=0)
    first = env.publications.calls[0]
    env.compatibility.prepare_bundle.side_effect = AssertionError('retry must not prepare latest')
    actual = await env.gateway.read(SOURCE, index=0)
    again = await env.gateway.read(SOURCE, index=0)
    assert actual == again and all(manifest == first for manifest in env.publications.calls)
    env.compatibility.prepare_bundle.assert_awaited_once()
    env.extractor.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_real_output_trace_is_explicit_and_does_not_run_private_projection():
    env = await environment()
    env.publications.omit_trace = True
    with pytest.raises(DocumentServiceError, match='actual output reference'):
        await env.gateway.read(SOURCE, index=0)
    env.extractor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['user', 'subject', 'name', 'document'])
async def test_original_payload_compatibility_shape_cannot_read_unknown_or_foreign_material(change):
    env = await environment()
    await env.gateway.read(SOURCE, index=0)
    scope = env.bundle.material.scope
    kwargs = {
        'document_id': env.source.document_id,
        'user_id': scope.user_id,
        'subject_id': scope.subject_id,
        'name': 'original_file',
    }
    kwargs[{'user': 'user_id', 'subject': 'subject_id', 'name': 'name', 'document': 'document_id'}[change]] = 'unknown'
    with pytest.raises((PermissionError, DocumentServiceError)):
        await env.gateway.processor.read_document_payload(**kwargs)
    assert env.materials.read_original.await_count == 1


@pytest.mark.asyncio
async def test_prepared_attachment_identity_cannot_change_on_retry():
    env = await environment()
    await env.gateway.read(SOURCE, index=0)
    with pytest.raises(DocumentServiceError, match='attachment changed'):
        await env.gateway.read(RemoteFileInput(url='https://example.test/another.pdf', file_type='pdf'), index=0)
    env.compatibility.prepare_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_retry_stays_at_the_prepare_boundary_and_keeps_call_audit():
    env = await environment()
    error = DocumentParsingError('文件下载失败或超时')
    error.__cause__ = httpx.ConnectError('offline synthetic transport failure')
    env.compatibility.prepare_bundle.side_effect = [error, env.bundle]
    result = await env.gateway.read(SOURCE, index=3)
    assert result.references[0]['fileIndex'] == 3
    assert env.compatibility.prepare_bundle.await_count == 2
    assert all(call.kwargs['file_index'] == 3 for call in env.compatibility.prepare_bundle.await_args_list)
    assert len(env.publications.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'revision,allowed', [('analysis-health-report-v5', True), ('analysis-health-report-v6', False)]
)
async def test_failed_material_uses_fixed_legacy_fallback_and_never_formal_publication(revision, allowed):
    env = await environment()
    error = DocumentInterpretationError('synthetic interpretation failure')
    env.compatibility.prepare_bundle.side_effect = error
    payload = env.bundle.material.payload
    payload.update(
        interpretationRevision=revision,
        validationRevision=revision,
        failureTrace={'traceId': 'fixed-failed-call', 'errorType': 'DocumentInterpretationError'},
    )
    material = replace(env.bundle.material, _payload_json=canonical_json(payload))
    env.materials.capture_failed_material.return_value = material
    if allowed:
        result = await env.gateway.read(SOURCE, index=0)
        assert result.references[0]['processingStatus'] == 'fallback_source_only'
        assert result.references[0]['traceId'] == 'fixed-failed-call'
        assert env.extractor.call_args.args[0][0].report == {}
    else:
        with pytest.raises(HealthReportUpstreamError):
            await env.gateway.read(SOURCE, index=0)
    assert not env.publications.calls
    call = env.materials.capture_failed_material.call_args.kwargs['call']
    assert call.entrypoint == 'report' and call.source_id == 'final' and call.file_index == 0
    assert call.file_url == SOURCE.url and call.session_id == 'session'


@pytest.mark.asyncio
async def test_factory_manifest_is_detached_and_does_not_execute_any_dependency():
    env = await environment()
    descriptor = env.factory.document_input_manifest()
    assert descriptor['consumerRevision'] == 'report-fixed-documents.v1'
    assert descriptor['inputSelection'] == asdict(UNDERSTANDING_SELECTION)
    assert descriptor['interpretationProjectionRevision'] == 'report-understanding-public.v1'
    assert descriptor['outputSelection'] == asdict(REPORT_FIXED_SELECTION)
    descriptor['sourceFetchPolicy']['backoffSeconds'].append(99)
    descriptor['outputSelection']['implementation_revision'] = 'changed'
    assert env.factory.document_input_manifest()['sourceFetchPolicy']['backoffSeconds'] == [0]
    assert env.factory.document_input_manifest()['outputSelection'] == asdict(REPORT_FIXED_SELECTION)
    env.compatibility.ensure_self_scope.assert_not_awaited()
    env.compatibility.prepare_bundle.assert_not_awaited()
    env.materials.read_original.assert_not_awaited()
    assert not env.publications.calls


@pytest.mark.asyncio
async def test_caller_mutation_during_prepare_cannot_change_failure_material_lookup():
    env = await environment()
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await release.wait()
        raise DocumentInterpretationError('synthetic interpretation failure')

    env.compatibility.prepare_bundle.side_effect = pending
    source = SOURCE.model_copy(deep=True)
    task = asyncio.create_task(env.gateway.read(source, index=0))
    await entered.wait()
    source.url = 'https://example.test/mutated.pdf'
    release.set()
    with pytest.raises(HealthReportUpstreamError):
        await task
    assert env.materials.capture_failed_material.call_args.kwargs['call'].file_url == SOURCE.url


def test_public_registration_helper_is_idempotent_and_keeps_the_existing_legacy_alias():
    from adapters.report.document_profiles import REPORT_LEGACY_SOURCE_SELECTION
    from adapters.report.document_profiles import configure_report_source_profiles
    from modules.analysis.application.output_profiles import DocumentOutputProfiles
    from modules.report.document_source import extract_intrinsic_report_source

    profiles = DocumentOutputProfiles()
    processor = SimpleNamespace(supports_document_reuse=True, output_profiles=profiles)
    configure_report_source_profiles(processor)
    configure_report_source_profiles(processor)
    profile = profiles.get(REPORT_LEGACY_SOURCE_SELECTION)
    assert profile.extractor is extract_intrinsic_report_source and profile.needs_bytes
    assert profiles.get('report_source') == profile
    existing = DocumentOutputProfiles()

    def other(snapshots):
        return {}

    existing.register_report_source(other, version='older')
    configure_report_source_profiles(SimpleNamespace(supports_document_reuse=True, output_profiles=existing))
    assert existing.get('report_source').version == 'older'
    assert existing.get(REPORT_LEGACY_SOURCE_SELECTION).extractor is extract_intrinsic_report_source


@pytest.mark.parametrize('wrong', ['extractor', 'bytes', 'reuse'])
def test_public_registration_conflicts_do_not_replace_the_registered_implementation(wrong):
    from adapters.report.document_profiles import configure_report_source_profiles
    from modules.analysis.application.output_profiles import DocumentOutputProfiles
    from modules.report.document_source import extract_intrinsic_report_source

    profiles = DocumentOutputProfiles()
    extractor = (lambda snapshots: {}) if wrong == 'extractor' else extract_intrinsic_report_source
    profiles.register_report_source(extractor, version='report-source-v16', needs_bytes=wrong != 'bytes')
    original = profiles.get('report_source')
    with pytest.raises(DocumentServiceError):
        configure_report_source_profiles(
            SimpleNamespace(supports_document_reuse=wrong != 'reuse', output_profiles=profiles)
        )
    assert profiles.get('report_source') is original


@pytest.mark.asyncio
async def test_mixed_raw_group_uses_only_materials_preserves_duplicates_and_never_publishes_group():
    env = await environment()
    await env.gateway.read(SOURCE, index=0)
    env.compatibility.prepare_bundle.side_effect = DocumentInterpretationError('synthetic failed second page')
    payload = env.bundle.material.payload
    payload['documentMetadata']['documentId'] = 'failed-other'
    payload['documentId'] = 'failed-other'
    payload.update(
        interpretationRevision='analysis-health-report-v5',
        validationRevision='analysis-health-report-v5',
        failureTrace={'traceId': 'fixed-failed-second', 'errorType': 'DocumentInterpretationError'},
    )
    env.materials.capture_failed_material.return_value = replace(
        env.bundle.material, _payload_json=canonical_json(payload)
    )
    await env.gateway.read(RemoteFileInput(url='https://example.test/second.pdf', file_type='pdf'), index=1)
    before = len(env.publications.calls)
    combined = await env.gateway.combine([0, 1, 1])
    inputs = env.extractor.call_args.args[0]
    assert [item.document_id for item in inputs] == [env.source.document_id, 'failed-other', 'failed-other']
    assert all(item.report == {} for item in inputs)
    assert len(env.publications.calls) == before == 1
    assert [item['fileIndex'] for item in combined.references] == [0, 1, 1]
    assert all(item['groupProcessingStatus'] == 'fallback_source_only' for item in combined.references)
