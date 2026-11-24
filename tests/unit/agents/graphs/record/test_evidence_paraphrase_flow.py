"""Natural evidence summaries must not exhaust a recoverable QA creation."""

import json
from collections import Counter

import pytest

from .test_creation_recovery import creation_manager
from .test_creation_recovery import drain
from .test_creation_recovery import receipt
from .test_generation_validation import report_processor

USER = 'user-1'
SESSION = 'session-1'
FILES = [{'url': 'https://example.test/synthetic-report.pdf'}]
REPORT_SUMMARY = '既往因消化不适就诊；本次检查提供菌群相关评估，尚需结合当前感受。'
REPORT_PARAPHRASES = ('以往曾因肠胃不舒服看过医生', '资料包含肠道微生态评估，需要结合现在的具体感受进一步了解')
ANSWER = '吃完饭有时会胀，我想知道是不是吃的东西有关。'
ANSWER_PARAPHRASE = '餐后偶有腹胀，希望了解与吃的食物是否有关'
CONCERN_PARAPHRASE = '想了解餐后不适与饮食的关联'


def paraphrase_case():
    processor = report_processor()
    processor.process.return_value.update(
        reportType='microbiome',
        aiSummary=REPORT_SUMMARY,
        processing={'status': 'partial', 'retryable': True, 'issueCount': 1},
    )
    case = creation_manager(health_report_processing_service=processor)
    original = case.gateway.content

    def respond(request):
        value = json.loads(original(request))
        payload, _ = json.JSONDecoder().raw_decode(request.content[request.content.index('{') :])
        if request.profile.name == 'record.plan':
            if payload['本次回答'] is None:
                assert REPORT_PARAPHRASES[0] not in payload['本次报告']
                value['intake']['facts'] = [
                    {
                        'dimension': 'medical_history',
                        'scope': 'general',
                        'status': 'known',
                        'summary': '既往有消化相关就诊经历',
                        'evidence': [{'source': '本次报告', 'quote': REPORT_PARAPHRASES[0]}],
                    }
                ]
            else:
                assert ANSWER_PARAPHRASE not in payload['本次回答']
                for complaint in value['intake']['complaints']:
                    complaint['evidence'] = [{'source': '本次回答', 'quote': ANSWER_PARAPHRASE}]
                value['concerns'] = [
                    {
                        'id': None,
                        'text': '餐后不适是否与饮食有关？',
                        'evidence': CONCERN_PARAPHRASE,
                        'readyToAnswer': False,
                        'informationNeeded': '不适通常在哪些饮食后出现',
                    }
                ]
        elif request.profile.name == 'record.question':
            current = payload['patient']['本次回答']
            if current is None:
                assert all(quote not in payload['patient']['本次报告'] for quote in REPORT_PARAPHRASES)
                value['assistantMessage'] = {
                    'acknowledgement': '资料里记载过消化相关就诊经历，我们先了解您现在最在意的感受。',
                    'rationale': '既往记录需要结合您目前的实际体验来理解。',
                    'evidence': [{'source': '本次报告', 'quote': quote} for quote in REPORT_PARAPHRASES],
                }
            else:
                value['assistantMessage'] = {
                    'acknowledgement': '进食后有时会腹胀，也希望了解是否与饮食有关。',
                    'rationale': '了解目前的感受及饮食背景，有助于理解这次情况。',
                    'evidence': [{'source': '本次回答', 'quote': ANSWER_PARAPHRASE}],
                }
        return json.dumps(value, ensure_ascii=False)

    case.gateway.content = respond
    return case, processor


def model_calls(case):
    return Counter(request.profile.name for request in case.gateway.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_paraphrased_evidence_reaches_first_question_and_next_turn_without_recovery(streaming):
    case, processor = paraphrase_case()
    first = await case.manager.createSession(USER, SESSION, file_list=FILES)
    if first['status'] == 'processing':
        await drain(case)
        first = await case.manager.getSession(USER, SESSION)
    assert first['status'] == 'question'
    assert first['interactionCount'] == 0 and first['questionType'] in {'radio', 'checkbox'}
    assert '消化相关就诊经历' in first['assistantMessage']
    assert first['documentProcessing'][0]['status'] == 'partial'
    assert model_calls(case) == {'record.plan': 1, 'record.question': 1}
    assert processor.process.await_count == 1
    snapshot = await receipt(case)
    assert snapshot['status'] == 'active' and 'qaCreation' not in snapshot
    assert snapshot['qaFlow']['intake']['facts']['general:medical_history']['summary'] == '既往有消化相关就诊经历'

    # Neither the idempotent create nor the actual recovery scan re-enters models.
    replay = await case.manager.createSession(USER, SESSION, file_list=FILES)
    assert replay['questionId'] == first['questionId']
    assert (await case.manager.getSession(USER, SESSION))['questionId'] == first['questionId']
    case.redis.now += 16
    assert await case.manager.recoverPendingCreations() == 0
    assert model_calls(case) == {'record.plan': 1, 'record.question': 1}

    assert ANSWER_PARAPHRASE not in ANSWER and CONCERN_PARAPHRASE not in ANSWER
    if streaming:
        events = [
            event
            async for event in case.manager.submitAnswerStream(
                USER, SESSION, first['questionId'], ANSWER, thinking=False
            )
        ]
        assert events[-1]['data']['type'] == 'finish'
        assert not any(event['data']['type'] == 'error' for event in events)
        current = next(event['data'] for event in events if event['data'].get('status') == 'question')
    else:
        current = await case.manager.submitAnswer(USER, SESSION, first['questionId'], ANSWER, thinking=False)
    assert current['status'] == 'question' and current['interactionCount'] == 1
    assert current['questionId'] != first['questionId']
    assert '进食后有时会腹胀' in current['assistantMessage']
    assert model_calls(case) == {'record.plan': 2, 'record.question': 2}
    assert processor.process.await_count == 1
    after = await receipt(case)
    assert after['qaHistory'][-1]['answer'] == ANSWER
    assert after['parsedHealthReports'] == snapshot['parsedHealthReports']
    assert any(item['evidence'] == CONCERN_PARAPHRASE for item in after['qaFlow']['concerns'])
    assert not any(key in current for key in ('evidence', 'diagnostics', 'generationDiagnostics'))
    repeated = await case.manager.submitAnswer(USER, SESSION, first['questionId'], ANSWER, thinking=False)
    assert repeated['questionId'] == current['questionId']
    assert model_calls(case) == {'record.plan': 2, 'record.question': 2}
