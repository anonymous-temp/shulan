"""Synchronous delivery, source isolation and shared request budgets."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.health_analysis.basic_info_sync import BasicInfoDeliveryError
from modules.health_analysis.basic_info_sync import BasicInfoUnavailableError
from modules.health_analysis.contracts import AnalysisBusyError
from modules.health_analysis.contracts import AnalysisGenerationError
from modules.health_analysis.contracts import AnalysisLimits
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import InvalidAnalysisInputError
from modules.health_analysis.contracts import SourceUnavailableError
from modules.health_analysis.contracts import TraceDeliveryError
from modules.health_analysis.contracts import TraceReceipt
from modules.health_analysis.policy import background_questions
from modules.health_analysis.policy import build_schema
from modules.health_analysis.trace import trace_hash
from tests.unit._support.health_analysis import payload
from tests.unit._support.health_analysis import schema
from tests.unit._support.health_analysis import setup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'report,chat,answer', [(r, c, a) for r in (False, True) for c in (False, True) for a in (False, True)]
)
async def test_eight_source_combinations_keep_available_material_and_confirm_trace(report, chat, answer):
    service, _, _, _, generator, writer = setup(has_profile=report, has_chat=chat)
    result = await service.generate(payload(answer), request_uuid='test-http-uuid')
    snapshot = generator.generate.call_args.args[0]
    assert snapshot['has_personal_data'] is (report or chat or answer)
    assert snapshot['has_historical_schemas'] is True
    assert len(snapshot['personal_materials']) == int(report) + int(chat) + int(answer)
    assert snapshot['profile_gender'] == 'male'
    assert writer.records[0]['uuid'] == 'test-http-uuid'
    assert writer.records[0]['result'] == result
    result['healthReview'] = '客户端修改'
    assert writer.records[0]['result']['healthReview'] != result['healthReview']


@pytest.mark.asyncio
async def test_missing_confirmation_provider_fails_before_any_source_or_model():
    service, _, profile, chat, generator, writer = setup()
    service.basic_info_sync.port = None
    with pytest.raises(BasicInfoDeliveryError):
        await service.generate(payload(), request_uuid='test')
    profile.read.assert_not_awaited()
    chat.read.assert_not_awaited()
    generator.generate.assert_not_awaited()
    assert writer.records == []


@pytest.mark.asyncio
async def test_unconfirmed_registration_never_reads_sources_or_runs_model():
    service, confirmation, profile, chat, generator, writer = setup()
    confirmation.register.side_effect = BasicInfoUnavailableError('synthetic unavailable')
    writer.write = AsyncMock(side_effect=TraceDeliveryError())
    with pytest.raises(BasicInfoDeliveryError):
        await service.generate(payload(), request_uuid='test')
    for dependency in (profile.read, chat.read, generator.generate, writer.write):
        dependency.assert_not_awaited()


@pytest.mark.asyncio
async def test_expected_source_failure_preserves_other_source_and_failure_category():
    service, _, profile, _, _, writer = setup(has_chat=True)
    profile.read.side_effect = SourceUnavailableError('upstream_failed')
    result = await service.generate(payload(), request_uuid='test')
    assert result['healthReview']
    assert writer.records[0]['sourceSnapshots']['profile']['coverage']['reason'] == 'upstream_failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [PermissionError('synthetic'), TypeError('synthetic program error')])
async def test_permission_and_program_errors_are_not_source_degradation(error):
    service, _, profile, chat, generator, writer = setup()
    stopped = asyncio.Event()

    async def pending(scope):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    chat.read.side_effect = pending
    profile.read.side_effect = error
    with pytest.raises(type(error)):
        await service.generate(payload(), request_uuid='test')
    assert stopped.is_set()
    generator.generate.assert_not_awaited()
    assert not writer.records


@pytest.mark.asyncio
async def test_source_timeout_is_local_and_total_deadline_cancels_work():
    limits = AnalysisLimits(total_timeout_seconds=0.2, source_timeout_seconds=0.01, trace_timeout_seconds=0.1)
    service, _, profile, _, _, writer = setup(limits=limits, has_chat=True)

    async def slow(scope):
        await asyncio.Event().wait()

    profile.read.side_effect = slow
    assert (await service.generate(payload(), request_uuid='test'))['healthReview']
    assert writer.records[0]['sourceSnapshots']['profile']['coverage']['reason'] == 'source_timeout'


@pytest.mark.asyncio
async def test_trace_confirmation_is_required_before_response_and_preserves_frozen_result():
    service, _, _, _, _, writer = setup()
    entered, release = asyncio.Event(), asyncio.Event()
    original = writer.write

    async def save(record):
        entered.set()
        await release.wait()
        return await original(record)

    writer.write = save
    task = asyncio.create_task(service.generate(payload(), request_uuid='test'))
    await entered.wait()
    assert not task.done()
    release.set()
    result = await task
    assert writer.records[0]['result'] == result


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', [None, TraceReceipt('wrong', 'wrong', True), TraceReceipt('wrong', 'wrong', False)])
async def test_unconfirmed_trace_never_returns_success(receipt):
    service, _, _, _, _, writer = setup()
    writer.write = AsyncMock(return_value=receipt)
    with pytest.raises(TraceDeliveryError):
        await service.generate(payload(), request_uuid='test')


@pytest.mark.asyncio
async def test_trace_retry_uses_same_run_and_content():
    service, _, _, _, _, writer = setup()
    original = writer.write
    calls = []

    async def flaky(record):
        calls.append(deepcopy(record))
        if len(calls) == 1:
            raise TraceDeliveryError('synthetic transient')
        return await original(record)

    writer.write = flaky
    await service.generate(payload(), request_uuid='test')
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.asyncio
async def test_cancel_inflight_sources_does_not_start_generation_and_releases_admission():
    service, _, profile, _, generator, writer = setup()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def pending(scope):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    profile.read.side_effect = pending
    task = asyncio.create_task(service.generate(payload(), request_uuid='test'))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and service._admitted == 0
    generator.generate.assert_not_awaited()
    assert not writer.records


@pytest.mark.asyncio
async def test_waiting_for_shared_slot_uses_deadline_without_starting_reads():
    limits = AnalysisLimits(total_timeout_seconds=0.02, source_timeout_seconds=0.01, trace_timeout_seconds=0.01)
    service, _, profile, chat, generator, writer = setup(limits=limits)
    service.request_semaphore = asyncio.Semaphore(0)
    with pytest.raises(AnalysisTimeoutError):
        await service.generate(payload(), request_uuid='test')
    for dependency in (profile.read, chat.read, generator.generate):
        dependency.assert_not_awaited()
    assert service._admitted == 0 and not writer.records


@pytest.mark.asyncio
async def test_waiting_capacity_rejects_without_unbounded_tasks():
    service, _, _, _, _, _ = setup(limits=AnalysisLimits(max_active_requests=1, max_waiting_requests=1))
    service._admitted = 2
    with pytest.raises(AnalysisBusyError):
        await service.generate(payload(), request_uuid='test')


@pytest.mark.asyncio
async def test_global_deadline_during_trace_returns_timeout_and_no_background_delivery():
    service, _, _, _, _, writer = setup(
        limits=AnalysisLimits(total_timeout_seconds=0.5, source_timeout_seconds=0.1, trace_timeout_seconds=0.5)
    )
    stopped = asyncio.Event()

    async def pending(record):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    writer.write = pending
    with pytest.raises(AnalysisTimeoutError):
        await service.generate(payload(), request_uuid='test')
    assert stopped.is_set() and service._admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'bad',
    [
        {},
        {'userId': 'test', 'groupId': 'test', 'surveyList': [], 'extra': True},
        {'userId': 0, 'groupId': 'test', 'surveyList': []},
    ],
)
async def test_invalid_request_does_not_reach_confirmation(bad):
    service, confirmation, *_ = setup()
    with pytest.raises(InvalidAnalysisInputError):
        await service.generate(bad, request_uuid='test')
    confirmation.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_generated_questionnaire_and_empty_data_analysis_are_rejected():
    service, _, _, _, generator, writer = setup()
    generator.generate.side_effect = None
    generator.generate.return_value = {
        'result': {
            'healthReview': '虚构个人评价',
            'suggestedMaterials': '',
            'advancedQuestions': '',
            'advancedSurveySchema': None,
        }
    }
    with pytest.raises(AnalysisGenerationError):
        await service.generate(payload(), request_uuid='test')
    assert not writer.records


@pytest.mark.asyncio
async def test_publication_accepts_unknown_option_without_a_confirmed_ban():
    service, _, _, _, generator, writer = setup()
    generator.generate.side_effect = None
    generator.generate.return_value = {
        'result': {
            'healthReview': '',
            'suggestedMaterials': '',
            'advancedQuestions': '',
            'advancedSurveySchema': schema(),
        }
    }
    result = await service.generate(payload(with_answer=True), request_uuid='test')
    assert result['advancedSurveySchema'] == schema()
    assert writer.records[0]['result'] == result


@pytest.mark.asyncio
async def test_raw_chat_candidate_does_not_override_adopted_empty_material_branch():
    service, _, _, _, generator, writer = setup(has_chat=True)
    generator.generate.side_effect = None
    generator.generate.return_value = {
        'result': {
            'healthReview': '',
            'suggestedMaterials': '',
            'advancedQuestions': '',
            'advancedSurveySchema': build_schema(background_questions()[:1]),
        },
        'trace': {'effectiveHasPersonalData': False, 'materialDecisions': [{'id': 'chat:1', 'accepted': False}]},
    }
    await service.generate(payload(), request_uuid='test')
    assert writer.records[0]['snapshot']['has_candidate_materials'] is True
    assert writer.records[0]['snapshot']['has_personal_data'] is False


@pytest.mark.asyncio
async def test_profile_scope_identifier_limit_is_validated_before_confirmation():
    service, confirmation, *_ = setup()
    value = payload()
    value['userId'] = 'x' * 257
    with pytest.raises(InvalidAnalysisInputError):
        await service.generate(value, request_uuid='test')
    confirmation.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_truthy_receipt_is_not_an_explicit_durable_confirmation():
    service, _, _, _, _, writer = setup()

    async def unconfirmed(record):
        return TraceReceipt(record['runId'], trace_hash(record), 1)

    writer.write = unconfirmed
    with pytest.raises(TraceDeliveryError):
        await service.generate(payload(), request_uuid='test')
