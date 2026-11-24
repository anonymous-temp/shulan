"""Readable report context, flexible evidence wording and private source diagnostics."""

import json

import pytest

from modules.record.application import flow
from modules.record.application.presentation import bounded_report_context
from modules.record.application.presentation import report_context_lines
from modules.record.application.presentation import validate_message
from modules.record.application.validation import RecordSourceError
from modules.record.application.validation import validation_diagnostics

from .test_creation_recovery import creation_manager
from .test_creation_recovery import receipt
from .test_dynamic_record import setup_manager
from .test_generation_validation import capture_logs
from .test_generation_validation import report_processor


def report():
    return {
        'title': '合成检查报告',
        'reportType': 'lab',
        'reportTime': None,
        'status': 'abnormal',
        'observations': [
            {'itemName': '正常项', 'resultValue': '2', 'flag': '正常'},
            {'itemName': '目标项目', 'resultValue': '<1', 'unit': '%', 'referenceRange': '0-5', 'flag': '偏低'},
        ],
    }


def patient(text):
    return {'本次报告': text, '本次回答': None, '历史问答': [], '长期健康画像': '', '附带健康资料': ''}


def message(quote, source='本次报告'):
    return flow.PatientMessage(
        acknowledgement='报告中有相关检查记录，我们结合您的实际情况了解。',
        rationale='先了解目前关注的体验。',
        evidence=[{'source': source, 'quote': quote}],
    )


def test_missing_sources_are_absent_in_model_context_not_quotable_placeholders():
    manager, _, _, _ = setup_manager()
    context = manager._patientContext({'qaFlow': flow.new_flow()})
    assert context['长期健康画像'] is None
    assert context['附带健康资料'] is None
    for source in ('长期画像', '附带资料'):
        with pytest.raises(RecordSourceError):
            validate_message(message('不能把缺失资料当作患者事实', source), context, action='open_intake')


def test_reports_are_complete_quotable_facts_with_no_nested_json_or_inferred_date():
    data = report()
    data['expansion'] = data['observations']
    data['sections'] = [{'observations': data['observations']}]
    text = bounded_report_context(data, 1, 12000)
    quote = '目标项目：<1 %；参考范围：0-5；标记：偏低'
    assert quote in text and text.count(quote) == 1
    assert text.index('目标项目') < text.index('正常项')
    assert not any(value in text for value in ('itemName', 'resultValue', '{', 'null', '日期：', '报告时间：'))
    validate_message(message(quote), patient(text), action='open_intake')
    # The decoded model-facing patient is exactly the validator's source.
    assert json.loads(json.dumps({'patient': patient(text)}, ensure_ascii=False))['patient']['本次报告'] == text


def test_452_results_are_bounded_at_whole_items_and_keep_later_short_items():
    data = report()
    data['observations'] += [
        {'itemName': f'合成项目{i}', 'resultValue': str(i), 'unit': 'mmol/L', 'referenceRange': '0-100'}
        for i in range(450)
    ]
    data['observations'].insert(0, {'itemName': '超长项', 'resultValue': '完整长描述' * 4000, 'flag': '异常'})
    header, facts = report_context_lines(data, 1)
    text = bounded_report_context(data, 1, 12000)
    assert len(text) <= 12000
    assert text.endswith('（仅展示可完整引用的部分资料，完整结果已保存）')
    assert '超长项' not in text and '目标项目：<1 %' in text
    assert all(line in header + facts for line in text.splitlines()[:-1])
    assert '合成项目449：' not in text


def test_section_dates_and_imaging_remain_separate_without_mirrored_facts():
    data = report()
    data['observations'] = [{'itemName': '项目', 'resultValue': '1', 'sourceSection': 3}]
    data['sections'] = [
        {'sectionIndex': 3, 'reportTime': '2026-09-01', 'observations': [{'itemName': '项目', 'resultValue': '1'}]},
        {'reportTime': '2026-09-02', 'expansion': {'findings': '合成影像描述', 'diagnosis': '合成影像意见'}},
    ]
    text = bounded_report_context(data, 1, 12000)
    assert text.count('项目：1；日期：2026-09-01') == 1
    assert '影像所见：合成影像描述；日期：2026-09-02' in text
    assert '影像诊断：合成影像意见；日期：2026-09-02' in text


def test_different_specimens_and_methods_are_not_merged():
    data = report()
    data['observations'] = [
        {'itemName': '葡萄糖', 'resultValue': '5.5', 'unit': 'mmol/L', 'specimen': '血清', 'method': '空腹'},
        {'itemName': '葡萄糖', 'resultValue': '5.5', 'unit': 'mmol/L', 'specimen': '尿液', 'method': '随机'},
    ]
    text = bounded_report_context(data, 1, 12000)
    assert '葡萄糖：5.5 mmol/L；标本：血清；方法：空腹' in text
    assert '葡萄糖：5.5 mmol/L；标本：尿液；方法：随机' in text


def test_historical_observations_and_expansion_can_be_complementary():
    data = report()
    data['expansion'] = [*data['observations'], {'itemName': '仅历史扩展项目', 'resultValue': '7'}]
    text = bounded_report_context(data, 1, 12000)
    assert text.count('目标项目：<1 %') == 1
    assert '仅历史扩展项目：7' in text


def test_assessment_and_measurement_kinds_are_preserved_only_when_present():
    data = report()
    data['observations'] = [
        {'itemName': '合成风险评分', 'resultValue': '35.56', 'kind': 'assessment'},
        {'itemName': '合成检测项目', 'resultValue': '1', 'kind': 'measurement'},
        {'itemName': '历史未分类项目', 'resultValue': '2'},
    ]
    _, facts = report_context_lines(data, 1)
    assert '合成风险评分：35.56；结果类型：评估' in facts
    assert '合成检测项目：1；结果类型：测量' in facts
    assert '历史未分类项目：2' in facts


def test_imaging_sections_preserve_explicit_exam_titles():
    data = report()
    data['sections'] = [
        {'title': title, 'reportTime': '2026-09-01', 'expansion': {'findings': '可见结节'}}
        for title in ('甲状腺检查', '肺部CT检查')
    ]
    _, facts = report_context_lines(data, 1)
    assert '影像所见：可见结节；日期：2026-09-01；检查：甲状腺检查' in facts
    assert '影像所见：可见结节；日期：2026-09-01；检查：肺部CT检查' in facts


def test_multiple_attachments_keep_separate_complete_contributions(monkeypatch):
    manager, module, _, _ = setup_manager()
    monkeypatch.setattr(module, 'MAX_QA_REPORT_CONTEXT_CHARS', 600)
    documents = [dict(report(), title=f'文件{i}', aiSummary='长摘要' * 200) for i in range(5)]
    text, status = manager._formatHealthReports(documents)
    assert len(text) <= 600 and status == 'abnormal'
    for i in range(5):
        assert f'标题：文件{i}' in text
    assert '长摘要' not in text


def test_evidence_allows_whitespace_formatting():
    source = patient('目标项目：<1   %；参考范围：0-5\n标记：偏低')
    validate_message(message('目标项目：<1 %；参考范围：0-5\t标记：偏低'), source, action='open_intake')


@pytest.mark.parametrize(
    'quote',
    [
        '目标项目检测结果为 <1 %，评价偏低。',
        '目标项目：<1 %；标记：偏低',
        '正常项为 2，目标项目低于 1%。',
    ],
)
def test_internal_report_evidence_can_paraphrase_or_summarize_noncontiguous_facts(quote):
    text = bounded_report_context(report(), 1, 12000)
    assert quote not in text
    validate_message(message(quote), patient(text), action='open_intake')


def test_internal_evidence_can_summarize_separate_answers_from_the_same_source():
    source = patient('项目：1')
    source['历史问答'] = [{'用户回答': '饭后偶尔腹胀'}, {'用户回答': '最近两周出现'}]
    validate_message(message('近两周偶有进食后腹胀。', '历史回答'), source, action='ask_intake')


@pytest.mark.parametrize('source_name', ['本次回答', '历史回答', '本次报告', '长期画像', '附带资料'])
@pytest.mark.parametrize('empty', [None, '', ' \n\t', [], {'内容': ' '}])
def test_evidence_cannot_claim_an_unavailable_source(source_name, empty):
    source = patient(None)
    if source_name == '历史回答':
        # AI questions and acknowledgement are not user answer sources.
        source['历史问答'] = [{'用户回答': empty, '问题': '私密甲', '承接': '私密甲'}]
    else:
        field = {'长期画像': '长期健康画像', '附带资料': '附带健康资料'}.get(source_name, source_name)
        source[field] = empty
    with pytest.raises(RecordSourceError) as caught:
        validate_message(message('私密甲', source_name), source, action='open_intake')
    details = validation_diagnostics(
        caught.value, schema=flow.TurnPresentation, stage='presentation', attempt=1, run_id='r', content='私密回复'
    )
    issue = details['issues'][0]
    assert issue['code'] == 'message_source_unavailable'
    assert issue['path'] == ['assistantMessage', 'evidence', 0, 'source']
    assert issue['source'] == source_name and issue['sourceAvailable'] is False
    assert '私密' not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_first_question_can_quote_rendered_report_fact_without_retry():
    processor = report_processor()
    processor.process.return_value = report()
    manager, _, gateway, _ = setup_manager(health_report_processing_service=processor)
    original = gateway.content

    def respond(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.question':
            value['assistantMessage'] = message('目标项目：<1 %').model_dump()
        return json.dumps(value, ensure_ascii=False)

    gateway.content = respond
    result = await manager.createSession('u', 's', file_list=[{'url': 'https://example.test/report.pdf'}])
    assert result['status'] == 'question'
    assert sum(request.profile.name == 'record.question' for request in gateway.requests) == 1


@pytest.mark.asyncio
async def test_unavailable_source_receipt_keeps_only_safe_diagnostics(monkeypatch, caplog):
    case = creation_manager(health_report_processing_service=report_processor())
    capture_logs(monkeypatch, caplog, case.module)
    original = case.gateway.content

    def respond(request):
        value = json.loads(original(request))
        if request.profile.name == 'record.question':
            value['assistantMessage']['evidence'] = [{'source': '长期画像', 'quote': '不能进入日志的合成健康原文999'}]
        return json.dumps(value, ensure_ascii=False)

    case.gateway.content = respond
    result = await case.manager.createSession('user-1', 'session-1', file_list=[{'url': 'https://example.test/r.pdf'}])
    assert result['status'] == 'question'
    assert (await receipt(case))['qaFlow']['generationQuality']['mode'] == 'partial'
    internal = next(
        json.loads(record.message.split('diagnostics=', 1)[1])
        for record in caplog.records
        if 'message_source_unavailable' in record.message
    )
    assert internal['issues'][0]['code'] == 'message_source_unavailable'
    assert internal['issues'][0]['path'] == ['assistantMessage', 'evidence', 0, 'source']
    assert internal['issues'][0]['source'] == '长期画像'
    assert '不能进入日志' not in json.dumps(internal, ensure_ascii=False) + caplog.text
    assert 'generationDiagnostics' not in json.dumps(result)
    requests = [r for r in case.gateway.requests if r.profile.name == 'record.question']
    assert len(requests) == 2 and 'message_source_unavailable' in requests[-1].content
