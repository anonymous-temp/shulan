"""Initial generation failures are diagnosable and repair receives the failed rule."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.record.application import creation
from modules.record.application import flow
from modules.record.application import intake
from modules.record.application.presentation import validate_message
from modules.record.application.validation import validation_diagnostics

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_creation_recovery import receipt
from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import setup_manager

REPORT_QUOTE = '既往因消化不适就诊'


def multi_complaint_assessment(indexes):
    return assessment(
        sufficient=False,
        intake={
            'complaints': [
                {'id': None, 'text': name, 'evidence': [{'source': '本次回答', 'quote': name}]}
                for name in ('消化不适', '排便变化')
            ],
            'priorityClarificationNeeded': False,
            'preferredIndex': None,
            'facts': [],
        },
        gaps=[
            {
                'topic': '消化与排便',
                'dimension': 'habits',
                'scope': 'complaint',
                'complaintIndexes': indexes,
                'informationNeeded': '近期饮食是否变化',
                'reason': '了解两种变化的相关饮食背景',
            }
        ],
    )


@pytest.mark.parametrize('indexes', [[-1], [2], [0, 0], [], [True]])
def test_gap_targets_reject_invalid_current_turn_complaint_indexes(indexes):
    snapshot = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}, 'qaHistory': []}
    with pytest.raises(ValueError):
        parsed = flow.PatientAssessment.model_validate(multi_complaint_assessment(indexes))
        flow.validate_assessment(parsed, snapshot, '消化不适、排便变化')


def test_new_complaint_indexes_resolve_once_and_cannot_be_forged_as_stable_ids():
    snapshot = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}, 'qaHistory': []}
    parsed = flow.PatientAssessment.model_validate(multi_complaint_assessment([0, 1]))
    flow.validate_assessment(parsed, snapshot, '消化不适、排便变化')
    draft, decision = flow.advance_flow(snapshot, parsed, answer='消化不适、排便变化')
    ids = [item['id'] for item in draft['qaFlow']['intake']['complaints']]
    assert decision['action'] == 'ask_intake'
    assert decision['target']['dimension'] == 'medical_history'
    resolved = intake.resolve_gaps(draft['qaFlow'], parsed.intake, parsed.gaps)
    assert resolved[0].complaintIds == ids
    assert draft['qaFlow']['intake']['preferredId'] is None
    assert parsed.gaps[0].complaintIds == []
    assert 'complaintIndexes' not in json.dumps(draft, ensure_ascii=False)
    raw = multi_complaint_assessment([0])
    raw['gaps'][0]['complaintIds'] = ['invented']
    with pytest.raises(ValueError):
        flow.PatientAssessment.model_validate(raw)


def report_processor():
    return SimpleNamespace(
        process=AsyncMock(
            return_value={
                'reportId': 'report-1',
                'reportType': 'lab',
                'aiSummary': REPORT_QUOTE,
            }
        )
    )


def capture_logs(monkeypatch, caplog, module):
    logger = logging.getLogger(__name__)
    monkeypatch.setattr(module, 'logger', logger)
    monkeypatch.setattr(creation, 'logger', logger)
    caplog.set_level(logging.WARNING, logger=logger.name)


def diagnose(content, schema=flow.TurnPresentation):
    with pytest.raises(ValueError) as caught:
        flow.parse_model_object(content, schema)
    return validation_diagnostics(
        caught.value, schema=schema, stage='presentation', attempt=1, run_id='run-1', content=content
    )


def test_schema_diagnostics_exclude_patient_input_and_untrusted_field_names():
    secret = '患者私密信息示例'
    details = diagnose(json.dumps({'assistantMessage': secret, secret: secret}, ensure_ascii=False))
    assert secret not in json.dumps(details, ensure_ascii=False)
    assert 'input' not in json.dumps(details)
    assert {'code': 'model_type', 'path': ['assistantMessage']} in details['issues']
    assert {'code': 'missing', 'path': ['responses']} in details['issues']
    assert {'code': 'extra_forbidden', 'path': ['<field>']} in details['issues']


def test_custom_schema_rule_retains_safe_hint_without_pydantic_input():
    content = json.dumps(
        {
            'assistantMessage': {'acknowledgement': '患者私密内容', 'rationale': '', 'evidence': []},
            'responses': [],
            'question': {'question': '当前情况？', 'questionType': 'radio', 'options': ['其他', '其他（请补充）']},
        },
        ensure_ascii=False,
    )
    details = diagnose(content)
    assert details['issues'] == [
        {
            'code': 'invalid_choice_options',
            'path': ['question'],
            'hint': '选择题选项不能为空或重复',
        }
    ]
    assert '患者私密内容' not in json.dumps(details, ensure_ascii=False)


def test_invalid_json_diagnostics_include_location_without_partial_content():
    content = '{"assistantMessage": "患者私密内容",\n "question": }'
    details = diagnose(content)
    assert details['replyChars'] == len(content)
    assert details['issues'][0]['code'] == 'invalid_json'
    assert details['issues'][0]['line'] == 2
    assert '患者私密内容' not in json.dumps(details, ensure_ascii=False)


def test_unknown_validator_message_and_excess_fields_do_not_leak_or_expand_logs():
    error = ValueError('患者私密内容')
    details = validation_diagnostics(
        error, schema=flow.PatientAssessment, stage='analysis', attempt=2, run_id='r', content='私密正文'
    )
    assert '私密' not in json.dumps(details, ensure_ascii=False)
    details = diagnose(json.dumps({f'患者字段{i}': '私密内容' for i in range(50)}))
    assert details['issueCount'] > 50 and len(details['issues']) == 8
    assert '患者字段' not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_first_report_facts_repair_scope_without_inventing_a_chief_complaint(monkeypatch, caplog):
    processor = report_processor()
    manager, module, gateway, _ = setup_manager(health_report_processing_service=processor)
    capture_logs(monkeypatch, caplog, module)
    original = gateway.content

    def response(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.plan':
            value['intake']['facts'] = [
                {
                    'dimension': 'medical_history',
                    'scope': 'general' if 'primary_complaint_required' in request.content else 'primary',
                    'status': 'known',
                    'summary': '既往相关就诊',
                    'evidence': [{'source': '本次报告', 'quote': REPORT_QUOTE}],
                }
            ]
        return json.dumps(value, ensure_ascii=False)

    gateway.content = response
    result = await manager.createSession('u', 's', file_list=[{'url': 'https://example.test/report.pdf'}])
    assert result['status'] == 'question' and result['interactionCount'] == 0
    state = cached(module)['qaFlow']['intake']
    assert state['primaryId'] is None and state['complaints'] == []
    assert state['facts']['general:medical_history']['summary'] == '既往相关就诊'
    analysis_requests = [r for r in gateway.requests if r.profile.name == 'record.plan']
    assert len(analysis_requests) == 2 and 'general' in analysis_requests[-1].content
    assert 'stage=analysis attempt=1' in caplog.text and 'primary_complaint_required' in caplog.text
    assert REPORT_QUOTE not in caplog.text


@pytest.mark.asyncio
async def test_report_acknowledgement_can_paraphrase_a_longer_internal_quote_without_retry():
    manager, _, gateway, _ = setup_manager(health_report_processing_service=report_processor())
    original = gateway.content

    def response(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.question':
            message = value['assistantMessage']
            message['acknowledgement'] = '报告中有过去因肠胃不舒服就医的记录，需要结合目前的实际感受了解。'
        return json.dumps(value, ensure_ascii=False)

    gateway.content = response
    result = await manager.createSession('u', 's', file_list=[{'url': 'https://example.test/report.pdf'}])
    assert result['status'] == 'question' and '过去因肠胃不舒服就医' in result['assistantMessage']
    requests = [r for r in gateway.requests if r.profile.name == 'record.question']
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'stage,defect,code',
    [
        ('analysis', 'json', 'invalid_json'),
        ('presentation', 'schema', 'model_type'),
        ('presentation', 'rule', 'intake_method_explanation_forbidden'),
    ],
)
async def test_repeated_invalid_opening_keeps_safe_diagnostics_and_publishes_fallback(
    stage, defect, code, monkeypatch, caplog
):
    processor = report_processor()
    case = creation_manager(health_report_processing_service=processor)
    capture_logs(monkeypatch, caplog, case.module)
    original = case.gateway.content

    def response(request):
        text = original(request)
        if request.profile.name != ('record.plan' if stage == 'analysis' else 'record.question'):
            return text
        if defect == 'json':
            return '{"患者情况理解":"患者私密内容",'
        value = json.loads(text)
        if defect == 'schema':
            value['assistantMessage'] = '患者私密内容'
        else:
            value['assistantMessage']['acknowledgement'] = '这些信息有助于判断身体不适与哪些情况有关。'
        return json.dumps(value, ensure_ascii=False)

    case.gateway.content = response
    result = await case.manager.createSession(
        'user-1', 'session-1', file_list=[{'url': 'https://example.test/report.pdf'}]
    )
    assert result['status'] == 'question' and result['interactionCount'] == 0
    assert not any(key in result for key in ('generation', 'diagnostics', 'issues'))
    snapshot = await receipt(case)
    assert snapshot['qaFlow']['generationQuality']['mode'] == ('fallback' if stage == 'analysis' else 'partial')
    assert not snapshot.get('qaCreation')
    assert not snapshot['qaHistory']
    requests = [
        r
        for r in case.gateway.requests
        if r.profile.name == ('record.plan' if stage == 'analysis' else 'record.question')
    ]
    assert len(requests) == 2
    case.redis.now += 6
    await case.manager.recoverPendingCreations()
    await drain(case)
    assert (await receipt(case))['currentQuestion']['questionId'] == result['questionId']
    assert code in caplog.text and f'"stage": "{stage}"' in caplog.text
    assert '患者私密内容' not in caplog.text and REPORT_QUOTE not in caplog.text
    assert any(code in r.content for r in case.gateway.requests)
    retry = next(r for r in case.gateway.requests if 'previousOutput' in r.content)
    feedback = json.JSONDecoder().raw_decode(retry.content[retry.content.index('{"issues":') :])[0]
    assert feedback['previousOutput'] == response(retry)
    assert feedback['previousOutputTruncated'] is False
    assert any(issue['code'] == code for issue in feedback['issues'])
    case.gateway.content = original
    case.redis.now += 6
    await case.manager.recoverPendingCreations()
    await drain(case)
    restored = await case.manager.getSession('user-1', 'session-1')
    assert restored['questionId'] == result['questionId']
    assert restored['status'] == 'question' and restored['interactionCount'] == 0
    assert case.module.collection_sessions.document is None
    assert processor.process.await_count == 1


def test_documented_examples_pass_real_schema_and_policy_in_their_stated_context():
    root = Path(__file__).resolve().parents[5]
    examples = []
    for name, schema in [('plan', flow.PatientAssessment), ('question', flow.TurnPresentation)]:
        prompt = (root / 'prompts' / 'record' / f'{name}.txt').read_text(encoding='utf-8')
        text = prompt.split('示例事实', 1)[1]
        value, _ = json.JSONDecoder().raw_decode(text[text.index('{') :])
        examples.append(flow.parse_model_object(json.dumps(value, ensure_ascii=False), schema))
    snapshot = {'qaFlow': flow.new_flow(), 'currentQuestion': {'questionId': 'q'}, 'qaHistory': []}
    answer = '我想知道饮食怎么调整'
    flow.validate_assessment(examples[0], snapshot, answer, sources={'本次回答': answer})
    draft, decision = flow.advance_flow(snapshot, examples[0], answer=answer)
    assert decision['target']['dimension'] == 'medical_history'
    assert draft['qaFlow']['intake']['complaints'] == []
    patient = {'本次报告': '', '本次回答': '父亲有糖尿病', '历史问答': [], '长期健康画像': '', '附带健康资料': ''}
    validate_message(examples[1].assistantMessage, patient, action='ask_intake')
    flow.validate_new_question(examples[1].question, action='ask_intake')
    assert examples[1].question.questionType == 'radio'
    assert len(examples[1].question.options) >= 2
