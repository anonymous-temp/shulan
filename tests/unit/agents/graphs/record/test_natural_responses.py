"""Production regressions: natural acknowledgement and recoverable model output."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from tornado.web import HTTPError

from modules.record.application import flow
from modules.record.application.validation import RecordGenerationError
from modules.record.application.validation import repair_feedback
from modules.record.application.validation import validation_diagnostics
from utils.exceptions import format_exception_summary
from web.handlers.record import QASessionAnswerHandler

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize(
    'answer,acknowledgement',
    [
        ('吃完饭偶尔不舒服', '进餐前后的生活安排值得结合起来了解，帮助梳理相关背景。'),
        ('说不清哪里不舒服，有点担心', '可以从容易回忆的日常安排开始，逐步了解饮食、活动和休息的背景。'),
    ],
)
async def test_natural_acknowledgement_succeeds_without_copying_or_retrying(answer, acknowledgement, streaming):
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    original = gateway.content

    def response(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.question':
            value['assistantMessage']['acknowledgement'] = acknowledgement
        return json.dumps(value, ensure_ascii=False)

    gateway.content = response
    count = len(gateway.requests)
    result = await submit(manager, current, answer, streaming=streaming)
    assert result['status'] == 'question'
    assert acknowledgement in result['assistantMessage']
    assert answer not in result['assistantMessage']
    assert len(gateway.requests) == count + 2
    assert result['interactionCount'] == 1
    assert cached(module)['qaHistory'][-1]['answer'] == answer


@pytest.mark.parametrize(
    'dimension,aspect,expected',
    [
        ('用药情况', '用药情况', 'medical_history'),
        ('用药情况', None, 'medical_history'),
        ('不适感受', '出现频率', 'symptoms'),
        ('家族病史', '既往病史', 'family_history'),
    ],
)
def test_gap_aspect_is_derived_from_the_known_dimension(dimension, aspect, expected):
    value = {'主题': '餐后不适', '内容': dimension, '待补充信息': '相关背景'}
    if aspect is not None:
        value['角度'] = aspect
    assert flow.InformationGap.model_validate(value).aspect == expected


@pytest.mark.parametrize('dimension', ['未支持的内容', ['用药情况']])
def test_unknown_or_malformed_dimension_is_still_rejected(dimension):
    with pytest.raises(ValueError):
        flow.InformationGap.model_validate(
            {
                '主题': '餐后不适',
                '内容': dimension,
                '角度': '既往病史',
                '待补充信息': '相关背景',
            }
        )


def test_conflicting_aliases_are_not_silently_discarded():
    with pytest.raises(ValueError):
        flow.InformationGap.model_validate(
            {
                '主题': '餐后不适',
                '内容': '用药情况',
                'dimension': 'family_history',
                '角度': '用药情况',
                '待补充信息': '相关背景',
            }
        )


@pytest.mark.asyncio
async def test_medication_in_aspect_field_does_not_fail_the_whole_turn():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(
        sufficient=False,
        gaps=[
            {
                'topic': '餐后不适',
                'aspect': '用药情况',
                'dimension': 'medication',
                'scope': 'primary',
                'reason': '了解近期相关用药背景',
                'informationNeeded': '近期是否用药',
            }
        ],
    )
    count = len(gateway.requests)
    result = await submit(manager, current, '吃完饭偶尔不舒服')
    assert result['status'] == 'question' and result['interactionCount'] == 1
    assert len(gateway.requests) == count + 2
    assert cached(module)['qaFlow']['focus']['aspect'] == 'medical_history'


def test_enum_repair_feedback_includes_allowed_values_without_patient_input():
    value = assessment(intent='患者私密文本')
    with pytest.raises(ValueError) as caught:
        flow.PatientAssessment.model_validate(value)
    details = validation_diagnostics(
        caught.value, schema=flow.PatientAssessment, stage='analysis', attempt=1, run_id='synthetic', content=''
    )
    assert 'answer' in details['issues'][0]['expected']
    assert '患者私密文本' not in json.dumps(details, ensure_ascii=False)


def test_failed_output_is_available_for_repair_as_bounded_data():
    details = {'issues': [{'code': 'invalid_json'}]}
    content = '{"患者私密文本":"不完整输出\ud800"' + 'x' * 30000
    feedback = repair_feedback(details, content=content)
    payload = json.loads(feedback[feedback.index('{') :])
    assert payload['previousOutputTruncated'] is True
    assert payload['previousOutput'] == content[: len(payload['previousOutput'])]
    assert len(payload['previousOutput']) <= 24000
    assert payload['issues'] == details['issues']
    feedback.encode('utf-8')
    assert 'previousOutput' not in details


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error,status',
    [
        (RecordGenerationError('回应内容校验失败，请重试', {'stage': 'presentation', 'issues': []}), 502),
        (RuntimeError('保存会话失败'), 500),
    ],
)
async def test_answer_handler_distinguishes_rejected_model_output_from_internal_failure(error, status):
    handler = SimpleNamespace(handle_answer=AsyncMock(side_effect=error))
    with patch('web.handlers.record.logger'), pytest.raises(HTTPError) as caught:
        await QASessionAnswerHandler.post(handler)
    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_streamed_rejected_model_output_publishes_a_saved_conservative_question():
    manager, module, gateway, _ = setup_manager()
    current = await manager.createSession('u', 's')
    gateway.invalid_response = 2
    events = [
        event async for event in manager.submitAnswerStream('u', 's', current['questionId'], '偶尔不适', thinking=True)
    ]
    assert events[-1]['code'] == 200 and events[-1]['data']['type'] == 'finish'
    assert any(event['data']['type'] == 'reasoning' for event in events)
    question = next(event['data'] for event in events if event['data']['type'] == 'question')
    assert cached(module)['currentQuestion']['questionId'] == question['questionId']
    assert cached(module)['qaHistory'][-1]['answer'] == '偶尔不适'
    assert cached(module)['qaFlow']['generationQuality']['mode'] == 'partial'


def test_http_exception_summary_cannot_expose_pydantic_input_through_the_error_chain():
    secret = '患者私密文本'
    try:
        try:
            flow.PatientAssessment.model_validate({'意图': secret})
        except ValueError as error:
            raise RecordGenerationError('患者信息分析结果无效，请重试', {}) from error
    except RecordGenerationError as error:
        try:
            raise HTTPError(502, log_message=str(error)) from error
        except HTTPError as public:
            summary = format_exception_summary(public)
    assert secret not in summary
    assert 'input_value' not in summary
    assert 'ValidationError' in summary
