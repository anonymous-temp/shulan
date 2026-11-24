"""Attachment quality does not block intake or change a published QA snapshot."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.application.service import HealthReportUpstreamError
from modules.record.application.fallback import OPENING_MESSAGE
from tests.unit.services.analysis.test_document_reuse import reuse_flow

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_creation_recovery import receipt
from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_record_agent import FakeHealthProfileMemoryProvider

MODEL_ACKNOWLEDGEMENT = '我们先从您当前最在意的身体感受聊起。'
MODEL_RATIONALE = '了解近期的变化有助于确定后续需要补充的情况。'
MODEL_OPENING = MODEL_ACKNOWLEDGEMENT
ATTACHMENT_CASES = [
    pytest.param(['complete'], None, id='complete'),
    pytest.param(['partial'], None, id='partial'),
    pytest.param(['complete', 'partial', 'unavailable'], None, id='mixed'),
    pytest.param(['unavailable'], None, id='unavailable_without_profile'),
    pytest.param(['unavailable'], '既往用户确认：偶有腹胀。', id='unavailable_with_profile'),
    pytest.param([], None, id='no_attachments'),
]
PUBLIC_MESSAGES = {
    'complete': '资料已读取。',
    'partial': '资料已部分读取，仅使用已确认的内容；您可以在问答中补充未读取的信息。',
    'unavailable': '这份资料暂时未能读取，您可以在问答中补充其中的重要内容。',
}


def fixed_opening(gateway):
    original = gateway.content

    def content(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.question':
            value['assistantMessage'] = {
                'acknowledgement': MODEL_ACKNOWLEDGEMENT,
                'rationale': MODEL_RATIONALE,
                'evidence': [],
            }
        return json.dumps(value, ensure_ascii=False)

    gateway.content = content


def patient_requests(gateway):
    result = []
    for request in gateway.requests:
        payload, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
        result.append((request.profile.name, payload if request.profile.name == 'record.plan' else payload['patient']))
    return result


class AttachmentProcessor:
    supports_document_reuse = True

    def __init__(self, values):
        self.values = values
        self.calls = []

    async def process(self, **kwargs):
        return (await self.process_document(**kwargs)).report

    async def process_document(self, **kwargs):
        self.calls.append(kwargs)
        index = kwargs['file_index']
        callback = kwargs.get('on_document_ready')
        if callback:
            await callback(f'document-{index}')
        value = self.values[index]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def report(index, status='complete'):
    return ProcessedHealthReport(
        report={
            'reportId': f'report-{index}',
            'reportType': 'lab',
            'title': f'资料{index}',
            'aiSummary': f'可信内容{index}',
            'status': 'normal',
            'observations': [{'itemName': f'项目{index}', 'resultValue': '1'}],
            'expansion': [{'itemName': f'项目{index}', 'resultValue': '1'}],
            'processing': {'status': status, 'issues': [], 'retryable': status == 'partial'},
        },
        text='不应直接提供给问答的原始正文',
        document_id=f'document-{index}',
        trace_id=f'trace-{index}',
        ingestion_status='queued',
        ingestion_job_id=None,
        reused=False,
        source_revision=2,
    )


def wrapped_failure():
    error = HealthReportUpstreamError('failed')
    error.__cause__ = DocumentInterpretationError('no trusted result')
    return error


def files(count):
    return [{'url': f'https://example.test/report-{index}.pdf'} for index in range(count)]


@pytest.mark.asyncio
@pytest.mark.parametrize('statuses,profile_context', ATTACHMENT_CASES)
async def test_attachment_status_does_not_prefix_the_model_opening(statuses, profile_context):
    processor = AttachmentProcessor(
        [
            wrapped_failure() if status == 'unavailable' else report(index, status)
            for index, status in enumerate(statuses)
        ]
    )
    manager, module, gateway, _ = setup_manager(
        health_report_processing_service=processor,
        health_profile_memory_provider=FakeHealthProfileMemoryProvider(profile_context),
    )
    fixed_opening(gateway)
    current = await manager.createSession('u', 's', file_list=files(len(statuses)))
    assert current['assistantMessage'] == MODEL_OPENING
    expected = []
    for index, status in enumerate(statuses):
        item = {
            'fileIndex': index,
            'status': status,
            'message': PUBLIC_MESSAGES[status],
            'retryable': status != 'complete',
        }
        if status != 'unavailable':
            item.update(documentId=f'document-{index}', sourceRevision=2)
        expected.append(item)
    assert current['documentProcessing'] == expected
    assert cached(module)['documentProcessing'] == expected
    assert cached(module)['healthProfileContext'] == profile_context


@pytest.mark.asyncio
@pytest.mark.parametrize('statuses,profile_context', ATTACHMENT_CASES)
@pytest.mark.parametrize('stage', ['record.plan', 'record.question'])
@pytest.mark.parametrize('failure', ['invalid_output', 'timeout'])
async def test_opening_fallback_is_neutral_and_replays_without_changing_processing(
    statuses, profile_context, stage, failure
):
    processor = AttachmentProcessor(
        [
            wrapped_failure() if status == 'unavailable' else report(index, status)
            for index, status in enumerate(statuses)
        ]
    )
    manager, module, gateway, _ = setup_manager(
        health_report_processing_service=processor,
        health_profile_memory_provider=FakeHealthProfileMemoryProvider(profile_context),
    )
    original = gateway.complete

    async def complete(request):
        if request.profile.name == stage and failure == 'timeout':
            gateway.requests.append(request)
            raise TimeoutError('synthetic model timeout')
        return await original(request)

    gateway.complete = complete
    if failure == 'invalid_output':
        if stage == 'record.plan':
            gateway.invalid_analysis = 2
        else:
            gateway.invalid_response = 2
    attachments = files(len(statuses))
    current = await manager.createSession('u', 's', file_list=attachments)
    assert current['assistantMessage'] == OPENING_MESSAGE
    assert current['status'] == 'question' and current['interactionCount'] == 0
    assert current['questionType'] in ('radio', 'checkbox') and len(current['options']) >= 2
    assert '文字或语音' in current['question']
    assert [item['status'] for item in current['documentProcessing']] == statuses
    assert [item['message'] for item in current['documentProcessing']] == [
        PUBLIC_MESSAGES[status] for status in statuses
    ]
    snapshot = deepcopy(cached(module))
    assert snapshot['healthProfileContext'] == profile_context
    assert len(snapshot['parsedHealthReports']) == sum(status != 'unavailable' for status in statuses)
    quality = snapshot['qaFlow']['generationQuality']
    assert quality['failureKind'] == ('timeout' if failure == 'timeout' else 'validation')
    assert quality['mode'] == ('fallback' if stage == 'record.plan' else 'partial')
    assert quality['unprocessedQuestionIds'] == []
    calls = len(gateway.requests)
    assert await manager.getSession('u', 's') == current
    assert (await manager.getHistory('u', 's'))['documentProcessing'] == current['documentProcessing']
    assert await manager.createSession('u', 's', file_list=attachments) == current
    assert cached(module) == snapshot
    assert len(gateway.requests) == calls and len(processor.calls) == len(statuses)


@pytest.mark.asyncio
@pytest.mark.parametrize('statuses,profile_context', ATTACHMENT_CASES)
async def test_both_model_stages_receive_only_internal_attachment_status(statuses, profile_context):
    processor = AttachmentProcessor(
        [
            wrapped_failure() if status == 'unavailable' else report(index, status)
            for index, status in enumerate(statuses)
        ]
    )
    manager, _, gateway, _ = setup_manager(
        health_report_processing_service=processor,
        health_profile_memory_provider=FakeHealthProfileMemoryProvider(profile_context),
    )
    fixed_opening(gateway)
    current = await manager.createSession('u', 's', file_list=files(len(statuses)))
    requests = patient_requests(gateway)
    assert [stage for stage, _ in requests] == ['record.plan', 'record.question']
    for _, patient in requests:
        assert '资料读取说明' not in patient
        assert patient['附件资料状态'] == [
            {'fileIndex': index, 'status': status} for index, status in enumerate(statuses)
        ]
        assert patient['长期健康画像'] == profile_context
        encoded = json.dumps(patient, ensure_ascii=False)
        assert all(item['message'] not in encoded for item in current['documentProcessing'])
        assert all(
            field not in encoded
            for field in ('"message"', '"retryable"', '"documentId"', '"sourceRevision"', '"traceId"')
        )
        if not statuses or all(status == 'unavailable' for status in statuses):
            assert patient['本次报告'] is None


@pytest.mark.asyncio
async def test_partial_and_unavailable_attachments_continue_with_only_trusted_results_and_fixed_snapshot():
    processor = AttachmentProcessor([report(0), report(1, 'partial'), wrapped_failure()])
    manager, module, gateway, _ = setup_manager(health_report_processing_service=processor)
    fixed_opening(gateway)
    current = await manager.createSession('u', 's', file_list=files(3))
    assert current['status'] == 'question'
    assert [item['status'] for item in current['documentProcessing']] == ['complete', 'partial', 'unavailable']
    assert [item['fileIndex'] for item in current['documents']] == [0, 1]
    assert current['documentProcessing'][1]['sourceRevision'] == 2
    assert current['assistantMessage'] == MODEL_OPENING
    snapshot = cached(module)
    assert snapshot['hasHealthReport']
    assert [item['reportId'] for item in snapshot['parsedHealthReports']] == ['report-0', 'report-1']
    assert '不应直接提供给问答的原始正文' not in str([request.content for request in gateway.requests])
    processor.values = [report(0), report(1), report(2)]
    assert (await manager.getSession('u', 's'))['documentProcessing'] == current['documentProcessing']
    assert (await manager.getHistory('u', 's'))['documentProcessing'] == current['documentProcessing']
    assert (await manager.createSession('u', 's', file_list=files(3))) == current
    assert len(processor.calls) == 3


@pytest.mark.asyncio
async def test_all_unavailable_attachments_continue_without_inventing_a_report():
    processor = AttachmentProcessor([wrapped_failure(), DocumentParsingError('OCR unavailable')])
    manager, module, gateway, _ = setup_manager(health_report_processing_service=processor)
    fixed_opening(gateway)
    current = await manager.createSession('u', 's', file_list=files(2))
    assert current['status'] == 'question'
    assert current['documents'] == []
    assert all(item['status'] == 'unavailable' for item in current['documentProcessing'])
    assert current['assistantMessage'] == MODEL_OPENING
    assert cached(module)['parsedHealthReports'] == []
    assert cached(module)['hasHealthReport'] is False
    assert manager._patientContext(cached(module))['本次报告'] is None
    assert len(gateway.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [True, False])
async def test_legacy_missing_attachment_status_is_projected_as_empty_without_changing_snapshot(missing):
    manager, module, _, _ = setup_manager()
    await manager.createSession('u', 's')
    snapshot = cached(module)
    if missing:
        snapshot.pop('documentProcessing')
    else:
        snapshot['documentProcessing'] = None
    before = deepcopy(snapshot)
    assert manager._patientContext(snapshot)['附件资料状态'] == []
    assert manager._presentationContext(snapshot, snapshot, None)['附件资料状态'] == []
    assert snapshot == before


@pytest.mark.asyncio
async def test_published_legacy_notice_and_processing_remain_unchanged_on_queries_and_creation_replay(monkeypatch):
    processor = AttachmentProcessor([report(0, 'partial')])
    manager, module, gateway, _ = setup_manager(health_report_processing_service=processor)
    fixed_opening(gateway)
    await manager.createSession('u', 's', file_list=files(1))
    snapshot = cached(module)
    published = (
        '这次上传的资料有部分内容未能读取，我会先结合已确认的信息继续建档，您也可以补充缺少的内容。\n\n' + MODEL_OPENING
    )
    snapshot['currentQuestion']['assistantMessage'] = published
    previous_question = deepcopy(snapshot['currentQuestion'])
    previous_question['questionId'] = 'previous-question'
    snapshot['qaHistory'] = [
        {'question': previous_question, 'answer': '餐后有不适', 'answeredAt': snapshot['createdAt']}
    ]
    snapshot['qaFlow'].update(interactionCount=1, intakeCount=1)
    snapshot['documentProcessing'][0]['message'] = '此前已经发布的附件处理说明。'
    save_cache(module, snapshot)
    stored = module.redis_client.value
    cache_write = AsyncMock(side_effect=AssertionError('read/replay must not rewrite the published snapshot'))
    document_write = AsyncMock(side_effect=AssertionError('read/replay must not rewrite a stored session'))
    monkeypatch.setattr(module.redis_client, 'setex', cache_write)
    monkeypatch.setattr(module.collection_sessions, 'replace_one', document_write)
    processor.values = [report(0)]
    current = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    replayed = await manager.createSession('u', 's', file_list=files(1))
    assert current['assistantMessage'] == replayed['assistantMessage'] == published
    assert history['history'][0]['assistantMessage'] == published
    for result in (current, history, replayed):
        assert result['documentProcessing'] == snapshot['documentProcessing']
    assert module.redis_client.value == stored
    assert len(gateway.requests) == 2 and len(processor.calls) == 1
    cache_write.assert_not_awaited()
    document_write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('expansion', [None, [], {}])
async def test_unknown_partial_report_keeps_direct_observations_in_question_context(expansion):
    partial = report(0, 'partial')
    partial.report.update(
        reportType='unknown',
        status='pending',
        aiSummary='已提取 1 项报告结果。',
        expansion=expansion,
        observations=[{'itemName': '尿酸', 'resultValue': '468', 'unit': 'μmol/L'}],
    )
    processor = AttachmentProcessor([partial])
    manager, module, gateway, _ = setup_manager(health_report_processing_service=processor)
    current = await manager.createSession('u', 's', file_list=files(1))
    assert current['status'] == 'question'
    snapshot = cached(module)
    assert snapshot['parsedHealthReports'][0]['reportType'] == 'unknown'
    context = manager._patientContext(snapshot)['本次报告']
    assert '尿酸' in context and '468' in context and 'μmol/L' in context
    assert all('468' in request.content and '尿酸' in request.content for request in gateway.requests)
    assert current['documentProcessing'][0]['status'] == 'partial'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        DocumentNotFoundError('not owned'),
        PermissionError('forbidden'),
        RuntimeError('Mongo save failed'),
        DocumentSubmissionConflictError('submission uncertain'),
    ],
)
async def test_authorization_persistence_and_submission_conflicts_are_not_attachment_degradation(error):
    processor = AttachmentProcessor([error])
    manager, module, gateway, _ = setup_manager(health_report_processing_service=processor)
    with pytest.raises(type(error)):
        await manager.createSession('u', 's', file_list=files(1))
    assert module.redis_client.value is None
    assert not gateway.requests


@pytest.mark.asyncio
async def test_settled_unavailable_attachment_is_not_retried_after_opening_fallback():
    processor = AttachmentProcessor([wrapped_failure()])
    flow = creation_manager(health_report_processing_service=processor)
    flow.gateway.invalid_response = 2
    first = await flow.manager.createSession('user-1', 'session-1', file_list=files(1))
    assert first['status'] == 'question'
    saved = await receipt(flow)
    assert not saved['documents'] and 'qaCreation' not in saved
    assert saved['documentProcessing'][0]['status'] == 'unavailable'
    assert saved['documentProcessing'][0]['documentId'] == 'document-0'
    processor.values = [report(0)]
    flow.redis.now += 16
    await flow.manager.recoverPendingCreations()
    await drain(flow)
    recovered = await flow.manager.getSession('user-1', 'session-1')
    assert recovered['status'] == 'question'
    assert recovered['questionId'] == first['questionId']
    assert recovered['documentProcessing'][0]['status'] == 'unavailable'
    assert len(processor.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', ['configuration', 'authentication', 'submission'])
async def test_non_content_parser_failure_is_preserved(marker):
    failure = DocumentParsingError('service unavailable')
    if marker == 'configuration':
        failure.content_failure = False
    elif marker == 'authentication':
        failure.__cause__ = RuntimeError('provider rejected credentials')
        failure.__cause__.status_code = 401
    else:
        failure.submission_unknown = True
    error = HealthReportUpstreamError('failed')
    error.__cause__ = failure
    processor = AttachmentProcessor([error])
    manager, _, gateway, _ = setup_manager(health_report_processing_service=processor)
    with pytest.raises(HealthReportUpstreamError):
        await manager.createSession('u', 's', file_list=files(1))
    assert not gateway.requests


@pytest.mark.asyncio
async def test_duplicate_files_in_one_creation_do_not_resume_the_same_settled_document(monkeypatch):
    documents = reuse_flow()
    documents.graph.content = '{"observations":['
    manager, module, _, _ = setup_manager(health_report_processing_service=documents.service)
    # Exercise the second input only after the first has settled, not while it is in flight.
    monkeypatch.setattr(module, 'QA_REPORT_PARSE_CONCURRENCY', 1)
    current = await manager.createSession('u', 's', file_list=files(2))
    assert current['status'] == 'question'
    assert [item['status'] for item in current['documentProcessing']] == ['unavailable', 'unavailable']
    assert len(documents.client.submit_calls) == 1
    assert next(iter(documents.index_collection.documents.values()))['interpretationRound'] == 1
