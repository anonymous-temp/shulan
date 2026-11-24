"""Current Analysis preparation is one algorithm; archive reads are pure and fixed."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.service import HealthReportProcessingService
from modules.analysis.preparation import payload_digest
from tests.unit._support.current_document import report
from tests.unit.services.analysis.test_basic_info_current import TEXT
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import extended_flow
from tests.unit.services.analysis.test_document_workflow import install_graph


def current_flow(*, configured=True):
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': TEXT}
    install_graph(flow, lambda _: report())
    runtime = flow.interpreter._agent_runtime
    runtime.registry.register(
        CurrentHealthReportInterpreter.CAPABILITY, flow.graph, version=CurrentHealthReportInterpreter.VERSION
    )
    current = CurrentHealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=flow.client)
    current.analyze = AsyncMock(wraps=current.analyze)
    old_service = flow.service
    flow.service = HealthReportProcessingService(
        interpreter=flow.interpreter,
        current_interpreter=current if configured else None,
        report_source=flow.source,
        health_profile_service=old_service._health_profile_service,
        document_trace_store=flow.traces,
        document_index=flow.index,
        document_downloader=flow.downloader,
        document_output_index=flow.output_index,
        output_profiles=old_service.output_profiles,
    )
    flow.current = current
    return flow


def stored_state(flow):
    return deepcopy(
        (
            flow.index_collection.documents,
            flow.output_index._collection.documents,
            flow.collection.documents,
            flow.traces.documents,
            flow.traces.payloads,
            flow.jobs.jobs,
        )
    )


@pytest.mark.asyncio
async def test_current_prepare_is_full_once_without_version_choice_or_publication():
    flow = current_flow()
    result = await flow.service.prepare_document(**request(subject_id='subject-self'))
    assert result.preparation_binding.input_snapshot.interpretation_revision == flow.current.VERSION
    assert result.output_selection.implementation_revision == flow.current.VERSION
    assert result.report['basicInfo']['age'] == 42
    assert result.report['observations']
    assert result.ingestion_status == 'not_submitted' and result.source_revision is None
    assert not flow.collection.documents and not flow.jobs.jobs
    flow.current.analyze.assert_awaited_once()
    assert len(flow.graph.calls) == len(flow.client.submit_calls) == 1
    assert flow.interpreter.VERSION == 'analysis-health-report-v12'


@pytest.mark.asyncio
async def test_current_prepare_same_file_reuses_the_current_fixed_execution():
    flow = current_flow()
    first = await flow.service.prepare_document(**request(subject_id='subject-self'))
    repeated = await flow.service.prepare_document(
        user_id='user-1', subject_id='subject-self', document_id=first.document_id
    )
    assert repeated.preparation_binding == first.preparation_binding
    assert repeated.output_selection == first.output_selection
    assert repeated.report == first.report
    flow.current.analyze.assert_awaited_once()
    assert len(flow.client.submit_calls) == 1
    assert not flow.collection.documents and not flow.jobs.jobs


@pytest.mark.asyncio
async def test_complete_current_preparation_rejects_hint_without_an_extra_extraction():
    flow = current_flow()
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    before = stored_state(flow)
    calls = flow.downloader.download.await_count, len(flow.client.submit_calls), len(flow.graph.calls)
    with pytest.raises(ValueError):
        await flow.service.prepare_document(
            user_id='user-1', subject_id='subject-self', document_id=prepared.document_id, report_type_hint='lab'
        )
    assert stored_state(flow) == before
    assert (flow.downloader.download.await_count, len(flow.client.submit_calls), len(flow.graph.calls)) == calls
    flow.current.analyze.assert_awaited_once()


@pytest.mark.parametrize(
    'extra',
    [
        {'interpretation_revision': 'analysis-health-report-v12'},
        {'output_selection': 'current'},
        {'publish': True},
        {'report_type_hint': 'lab'},
        {'_interpreter': object()},
        {'_current_interpreter': object()},
    ],
)
@pytest.mark.asyncio
async def test_current_prepare_disallows_execution_or_publication_switches_before_io(extra):
    flow = current_flow()
    before = stored_state(flow)
    with pytest.raises(ValueError):
        await flow.service.prepare_document(**request(subject_id='subject-self'), **extra)
    assert stored_state(flow) == before
    flow.downloader.download.assert_not_awaited()
    assert not flow.client.submit_calls and not flow.graph.calls


@pytest.mark.asyncio
async def test_missing_current_provider_fails_without_falling_back_to_the_temporary_default():
    flow = current_flow(configured=False)
    with pytest.raises(DocumentParsingError):
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    flow.downloader.download.assert_not_awaited()
    assert not flow.graph.calls and not flow.collection.documents


@pytest.mark.asyncio
async def test_read_prepared_round_returns_scoped_fixed_artifacts_without_effects():
    flow = current_flow()
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    binding = prepared.preparation_binding
    before = stored_state(flow)
    calls = len(flow.graph.calls), len(flow.client.submit_calls), flow.downloader.download.await_count
    frozen = await flow.service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=binding.input_snapshot.interpretation_round,
    )
    assert (frozen.user_id, frozen.subject_id, frozen.document_id) == ('user-1', 'subject-self', prepared.document_id)
    assert frozen.preparation_binding == binding and frozen.output_selection == prepared.output_selection
    assert payload_digest(frozen.parsed_document) == binding.parse_digest
    assert payload_digest(frozen.interpretation) == binding.interpretation_digest
    assert frozen.parsed_document['text'] == TEXT
    assert stored_state(flow) == before
    assert (len(flow.graph.calls), len(flow.client.submit_calls), flow.downloader.download.await_count) == calls


@pytest.mark.asyncio
async def test_fixed_read_never_substitutes_the_latest_index_or_another_owner():
    flow = current_flow()
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    binding = prepared.preparation_binding
    latest = flow.index_collection.documents[prepared.document_id]
    latest.update(
        interpretationRound=binding.input_snapshot.interpretation_round + 99,
        interpretationResultRound=binding.input_snapshot.interpretation_round + 99,
        payloadRefs={'docmind_result': 'unrelated-latest-parse', 'interpretation': 'unrelated-latest-interpretation'},
    )
    before = stored_state(flow)
    frozen = await flow.service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=binding.input_snapshot.interpretation_round,
    )
    assert frozen.preparation_binding == binding
    assert stored_state(flow) == before
    for user, subject in [('other-user', 'subject-self'), ('user-1', 'other-subject')]:
        with pytest.raises(DocumentNotFoundError):
            await flow.service.read_prepared_round(
                user_id=user,
                subject_id=subject,
                document_id=prepared.document_id,
                interpretation_round=binding.input_snapshot.interpretation_round,
            )
    assert len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_fixed_read_checks_archived_digests_without_repair_or_writes():
    flow = current_flow()
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    args = dict(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=prepared.preparation_binding.input_snapshot.interpretation_round,
    )
    original = await flow.index.get_archived_round(
        args['document_id'], args['user_id'], args['subject_id'], args['interpretation_round']
    )
    flow.index.get_archived_round = AsyncMock(return_value={**original, 'parseDigest': '0' * 64})
    before = stored_state(flow)
    with pytest.raises(DocumentParsingError):
        await flow.service.read_prepared_round(**args)
    assert stored_state(flow) == before and len(flow.graph.calls) == 1


@pytest.mark.parametrize('key', ['_current_interpreter', 'interpretation_revision'])
@pytest.mark.asyncio
async def test_default_port_cannot_be_used_as_an_execution_selector(key):
    flow = current_flow()
    value = flow.current if key == '_current_interpreter' else flow.current.VERSION
    before = stored_state(flow)
    with pytest.raises(ValueError):
        await flow.service.process_document(**request(**{key: value}))
    assert stored_state(flow) == before
    flow.downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_pending_io_recovery_does_not_switch_executor_or_resubmit_ocr():
    flow = current_flow()
    poll = flow.client.get_result
    flow.client.get_result = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic interrupted poll'))
    with pytest.raises(DocumentParsingError):
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    flow.client.get_result = poll
    result = await flow.service.prepare_document(**request(subject_id='subject-self'))
    assert result.output_selection.implementation_revision == flow.current.VERSION
    assert result.preparation_binding.input_snapshot.interpretation_revision == flow.current.VERSION
    flow.current.analyze.assert_awaited_once()
    assert len(flow.client.submit_calls) == 1
    assert not flow.collection.documents and not flow.jobs.jobs


@pytest.mark.asyncio
async def test_current_retryable_content_round_recovers_and_keeps_the_first_archive():
    from modules.analysis.application.documents import DocumentInterpretationError

    flow = current_flow()
    state = {'usable': False}
    install_graph(flow, lambda _: report() if state['usable'] else '{')
    with pytest.raises(DocumentInterpretationError):
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    document = next(iter(flow.index_collection.documents.values()))
    document_id, first_round = document['_id'], document['interpretationRound']
    first = await flow.service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=document_id,
        interpretation_round=first_round,
    )
    state['usable'] = True
    second = await flow.service.prepare_document(user_id='user-1', subject_id='subject-self', document_id=document_id)
    assert second.preparation_binding.input_snapshot.interpretation_round == first_round + 1
    assert second.report['basicInfo']['age'] == 42
    again = await flow.service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=document_id,
        interpretation_round=first_round,
    )
    assert again == first
    assert flow.current.analyze.await_count == 2 and len(flow.client.submit_calls) == 1
    assert not flow.collection.documents and not flow.jobs.jobs


def test_fixed_round_dto_is_exported_from_the_public_analysis_facade():
    from modules.analysis.application import PreparedDocumentRound
    from modules.analysis.application.documents import PreparedDocumentRound as Implementation

    assert PreparedDocumentRound is Implementation
