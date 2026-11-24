"""Conversational introductions preserve sources, flow and the public contract."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from health_profile.record_ingestion import _sanitized_answers
from modules.record.application import flow
from modules.record.application.dialogue import build_dialogue_context
from modules.record.application.presentation import acknowledgement_text
from modules.record.application.presentation import validate_acknowledgement_style
from modules.record.application.presentation import validate_message
from modules.record.application.presentation import validate_response_style
from tests.unit._support.harnesses import record_dialogue_acceptance as harness

from .test_dynamic_record import assessment
from .test_dynamic_record import cached
from .test_dynamic_record import save_cache
from .test_dynamic_record import setup_manager
from .test_dynamic_record import submit


def dialogue_gateway(gateway, *, edit=None):
    original = gateway.content

    def content(request):
        result = json.loads(original(request))
        if request.profile.name == 'record.question':
            payload = harness.request_payload(request)
            result['assistantMessage'].update(acknowledgement='接着聊聊您平时的生活。', rationale='', focusRef=None)
            if edit:
                edit(result, payload)
        return json.dumps(result, ensure_ascii=False)

    gateway.content = content


def test_rationale_is_optional_and_never_appended_to_public_introduction():
    message = flow.PatientMessage(acknowledgement='接着聊聊家里人的健康情况。', evidence=[])
    assert message.rationale == ''
    assert message.focusRef is None
    validate_acknowledgement_style(message, action='ask_intake')
    assert acknowledgement_text(message, action='ask_intake') == message.acknowledgement
    message.rationale = '旧版独立选题理由只供内部兼容。'
    assert acknowledgement_text(message, action='ask_intake') == message.acknowledgement


@pytest.mark.parametrize(
    'text',
    [
        '先了解医生明确诊断的慢性病，能让后续讨论更贴合实际。',
        '这些信息有助于判断身体不适是否与已有疾病相关，避免把指标异常误当成确诊。',
        '从您最想弄明白的具体问题说起，有助于我们一起理清重点。',
        '先了解您以往的疾病情况，这些信息有助于判断便秘可能与哪些因素有关。',
    ],
)
def test_intake_method_explanations_are_rejected(text):
    message = flow.PatientMessage(acknowledgement=text, evidence=[])
    with pytest.raises(ValueError) as error:
        validate_acknowledgement_style(message, action='ask_intake')
    assert error.value.code == 'intake_method_explanation_forbidden'


@pytest.mark.parametrize(
    'text',
    [
        '入睡这件事，我先了解您以往的疾病情况。',
        '接着聊聊家里人的健康情况。',
        '接下来看看临睡前这段时间的安排。',
    ],
)
def test_short_goal_reference_and_neutral_family_bridge_are_legal(text):
    validate_acknowledgement_style(flow.PatientMessage(acknowledgement=text, evidence=[]), action='ask_intake')


@pytest.mark.parametrize(
    'text',
    [
        '血亲有糖尿病并不意味着自己一定会患病。日常可保持规律进餐和活动，体检时关注血糖。',
        '血糖升高不等于已经确诊糖尿病。血糖检查有助于了解血糖变化，复查可由医生安排。',
    ],
)
def test_qa_explanations_and_legitimate_uncertainty_are_still_legal(text):
    validate_response_style(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_empty_rationale_publishes_without_extra_call_and_replay_is_unchanged(streaming):
    manager, module, gateway, _ = setup_manager()
    dialogue_gateway(gateway)
    first = await manager.createSession('u', 's')
    assert len(gateway.requests) == 2
    gateway.next_assessment = assessment(sufficient=False)
    before = len(gateway.requests)
    current = await submit(manager, first, '入睡困难', streaming=streaming)
    assert len(gateway.requests) == before + 2
    assert current['assistantMessage'] == '接着聊聊您平时的生活。'
    snapshot = deepcopy(cached(module))
    calls = len(gateway.requests)
    assert (await manager.getSession('u', 's'))['questionId'] == current['questionId']
    assert await submit(manager, first, '入睡困难', streaming=streaming) == current
    assert len(gateway.requests) == calls
    assert cached(module) == snapshot


@pytest.mark.asyncio
async def test_neutral_intake_bridge_needs_no_artificial_current_answer_evidence():
    manager, _, gateway, _ = setup_manager()
    dialogue_gateway(gateway, edit=lambda result, payload: result['assistantMessage'].update(evidence=[]))
    first = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    before = len(gateway.requests)
    result = await submit(manager, first, '入睡困难')
    assert len(gateway.requests) == before + 2
    assert result['assistantMessage'] == '接着聊聊您平时的生活。'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize(
    'text',
    [
        '为了更好地帮您分析和改善便秘问题，我需要先了解您有没有以下的疾病史。',
        '我想帮您看看便秘有哪些可以改善的方面。先了解您以往的疾病情况。',
        '针对便秘，先从您以往的疾病情况说起。',
        # These connectors have a concrete meaning, so they must not become banned words.
        '分析便秘时，我会把症状和以往的疾病情况放在一起考虑。',
        '为了帮您分析便秘，已经治愈的疾病也需要了解。',
    ],
)
async def test_user_facing_help_intent_publishes_with_grounded_focus_and_survives_replay(text, streaming):
    manager, module, gateway, _ = setup_manager()
    selected_refs = []

    def help_intent(result, payload):
        context = payload['patient']['对话衔接参考']
        if context['当前细项'] != 'medical_history':
            return
        goal = next(item for item in context['可用关注点'] if item['kind'] == 'goal')
        selected_refs.append(goal['ref'])
        result['assistantMessage'].update(
            acknowledgement=text,
            focusRef=goal['ref'],
            evidence=[{'source': goal['source'], 'quote': goal['quote']}],
        )

    dialogue_gateway(gateway, edit=help_intent)
    first = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    before = len(gateway.requests)
    current = await submit(manager, first, '便秘', streaming=streaming)
    assert len(gateway.requests) == before + 2
    assert current['assistantMessage'] == text
    assert current['question'].endswith('也可以通过文字或语音补充描述。')
    assert current['question'].count('也可以通过文字或语音补充描述。') == 1
    assert len(selected_refs) == 1
    state = deepcopy(cached(module))
    assert state['currentQuestion']['dialogueFocusRef'] == selected_refs[0]
    assert state['qaFlow']['generationQuality']['mode'] == 'normal'
    calls = len(gateway.requests)
    assert (await manager.getSession('u', 's'))['assistantMessage'] == text
    assert await submit(manager, first, '便秘', streaming=streaming) == current
    assert len(gateway.requests) == calls
    assert cached(module) == state


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_style_changes_do_not_rewrite_legacy_copy_user_answers_or_completed_receipts(streaming):
    manager, module, gateway, _ = setup_manager()
    dialogue_gateway(gateway)
    first = await manager.createSession('u', 's')
    raw_answer = '我也经常和家人一起散步。'
    gateway.next_assessment = assessment(sufficient=False)
    current = await submit(manager, first, raw_answer)
    snapshot = cached(module)
    legacy_intro = '我们也一起看看您平时的生活。'
    for question in [snapshot['currentQuestion'], snapshot['qaHistory'][0]['question']]:
        question.update(assistantMessage=legacy_intro, acknowledgementText=legacy_intro)
    receipt = snapshot['qaFlow']['lastSubmission']['result']
    receipt['assistantMessage'] = legacy_intro
    save_cache(module, snapshot)
    before = deepcopy(cached(module))
    calls = len(gateway.requests)

    restored = await manager.getSession('u', 's')
    history = await manager.getHistory('u', 's')
    replayed = await submit(manager, first, raw_answer, streaming=streaming)

    for response in [restored, replayed, *history['history']]:
        assert response['assistantMessage'] == legacy_intro
        assert response['question'].endswith('也可以通过文字或语音补充描述。')
    assert restored['questionId'] == replayed['questionId'] == current['questionId']
    assert replayed == ({**receipt, 'type': 'question'} if streaming else receipt)
    assert history['history'][0]['answer'] == raw_answer
    assert cached(module) == before
    assert len(gateway.requests) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_contextual_continuity_does_not_require_repeating_goal_or_creating_focus_evidence(streaming):
    manager, module, gateway, _ = setup_manager()
    contexts = []
    introductions = {
        'medical_history': '先了解您以往的疾病情况。',
        'family_history': '接着聊聊家里人的健康情况。',
        'diet_structure': '接下来看看平时的饮食。',
    }

    def continue_conversation(result, payload):
        context = payload['patient']['对话衔接参考']
        if context['当前细项'] not in introductions:
            return
        contexts.append(deepcopy(context))
        result['assistantMessage'].update(
            acknowledgement=introductions[context['当前细项']], focusRef=None, evidence=[]
        )

    dialogue_gateway(gateway, edit=continue_conversation)
    current = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    for dimension, answer in (
        ('medical_history', '便秘'),
        ('family_history', '没有疾病史'),
        ('diet_structure', '没有家族疾病史'),
    ):
        before = len(gateway.requests)
        current = await submit(manager, current, answer, streaming=streaming)
        assert len(gateway.requests) == before + 2
        assert current['assistantMessage'] == introductions[dimension]
        assert '便秘' not in current['assistantMessage']
        assert contexts[-1]['当前细项'] == dimension
        state = cached(module)
        assert state['currentQuestion'].get('dialogueFocusRef') is None
        assert state['qaFlow']['generationQuality']['mode'] == 'normal'
    assert contexts[-1]['已发布使用'] == []
    previous_turn = contexts[-1]['最近完整对话'][-1]
    assert previous_turn['AI发言'] == introductions['family_history']
    assert previous_turn['用户回答'] == '没有家族疾病史'
    assert any(turn['用户回答'] == '便秘' for turn in contexts[-1]['最近完整对话'])


def test_historical_goal_can_link_without_repeating_the_latest_answer():
    patient = {
        '本次回答': '没有家族疾病史。',
        '历史问答': [{'用户回答': '入睡困难'}],
        '本次报告': None,
        '长期健康画像': None,
        '附带健康资料': None,
        '基本信息': None,
    }
    intake = {
        'complaints': [
            {'id': 'sleep-goal', 'text': '入睡困难', 'evidence': [{'source': '历史回答', 'quote': '入睡困难'}]}
        ]
    }
    patient.update(build_dialogue_context(patient, {}, {'dimension': 'sleep'}, intake=intake))
    goal = patient['对话衔接参考']['可用关注点'][0]
    message = flow.PatientMessage(
        acknowledgement='入睡这件事，接下来看看临睡前这段时间的安排。',
        focusRef=goal['ref'],
        evidence=[{'source': '历史回答', 'quote': '入睡困难'}],
    )
    validate_message(message, patient, action='ask_intake')
    message.focusRef = None
    with pytest.raises(ValueError) as error:
        validate_message(message, patient, action='invite_supplement')
    assert error.value.code == 'current_answer_evidence_required'


@pytest.mark.asyncio
async def test_legacy_rationale_does_not_reappear_in_service_response():
    manager, _, gateway, _ = setup_manager()
    dialogue_gateway(
        gateway,
        edit=lambda result, payload: result['assistantMessage'].update(rationale='旧版独立理由不再对外展示。'),
    )
    result = await manager.createSession('u', 's')
    assert result['assistantMessage'] == '接着聊聊您平时的生活。'
    assert len(gateway.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_existing_repair_can_publish_two_sentence_help_intent_without_an_extra_review_call(streaming):
    manager, module, gateway, _ = setup_manager()
    attempts = []
    corrected = '我想帮您看看便秘有哪些可以改善的方面。先了解您以往的疾病情况。'

    def repair_introduction(result, payload):
        context = payload['patient']['对话衔接参考']
        if context['当前细项'] != 'medical_history':
            return
        goal = next(item for item in context['可用关注点'] if item['kind'] == 'goal')
        attempts.append(goal['ref'])
        result['assistantMessage'].update(
            acknowledgement='这些信息有助于判断便秘与已有疾病的关联。' if len(attempts) == 1 else corrected,
            focusRef=goal['ref'],
            evidence=[{'source': goal['source'], 'quote': goal['quote']}],
        )

    dialogue_gateway(gateway, edit=repair_introduction)
    first = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    before = len(gateway.requests)
    result = await submit(manager, first, '便秘', streaming=streaming)
    assert [request.profile.name for request in gateway.requests[before:]] == [
        'record.plan',
        'record.question',
        'record.question',
    ]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert result['assistantMessage'] == corrected
    state = cached(module)
    assert state['currentQuestion']['dialogueFocusRef'] == attempts[0]
    assert state['qaFlow']['generationQuality']['mode'] == 'repaired'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize(
    'invalid_text',
    [
        '这些信息有助于判断身体不适与已有疾病的关联。',
        '母亲和外祖父有糖尿病，您本人也确诊糖尿病，确实会关心遗传背景。',
        '先了解您以往的疾病情况。部分已提交内容尚未完成可靠分析，已按原文保留。',
        '先了解您以往的疾病情况。单凭现有信息无法准确判断。',
    ],
)
async def test_failed_intro_repair_preserves_valid_question_and_uses_neutral_fallback(streaming, invalid_text):
    manager, module, gateway, _ = setup_manager()
    generated = []

    def invalid_intro(result, payload):
        if payload['responseTask']['action'] == 'ask_intake':
            result['assistantMessage']['acknowledgement'] = invalid_text
            generated.append(deepcopy(result['question']))

    dialogue_gateway(gateway, edit=invalid_intro)
    first = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    before = len(gateway.requests)
    result = await submit(manager, first, '入睡困难', streaming=streaming)
    assert len(gateway.requests) == before + 3
    assert len(generated) == 2
    assert result['question'].startswith(generated[0]['question'])
    assert result['options'] == generated[0]['options']
    assert result['assistantMessage'] == '先聊聊您以往的疾病情况。'
    assert cached(module)['qaFlow']['generationQuality']['mode'] == 'partial'


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_published_focus_stays_internal_and_replay_never_increments_usage(streaming):
    manager, module, gateway, _ = setup_manager()
    references, contexts = [], []

    def choose_goal(result, payload):
        context = payload['patient']['对话衔接参考']
        contexts.append(deepcopy(context))
        if context['当前细项'] != 'medical_history':
            return
        goal = next(item for item in context['可用关注点'] if item['kind'] == 'goal')
        references.append(goal['ref'])
        result['assistantMessage'].update(
            acknowledgement='入睡这件事，我先了解您以往的疾病情况。', focusRef=goal['ref']
        )
        result['assistantMessage']['evidence'].append({'source': goal['source'], 'quote': goal['quote']})

    dialogue_gateway(gateway, edit=choose_goal)
    first = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    medical = await submit(manager, first, '入睡困难', streaming=streaming)
    assert len(references) == 1
    selected = references[0]
    assert cached(module)['currentQuestion']['dialogueFocusRef'] == selected
    assert selected not in json.dumps(medical, ensure_ascii=False)
    calls = len(gateway.requests)
    assert (await manager.getSession('u', 's'))['questionId'] == medical['questionId']
    assert await submit(manager, first, '入睡困难', streaming=streaming) == medical
    assert len(gateway.requests) == calls
    family = await submit(manager, medical, '没有疾病史', streaming=streaming)
    context = contexts[-1]
    assert context['当前细项'] == 'family_history' and context['可用关注点'] == []
    assert sum(row['count'] for row in context['已发布使用'] if row['ref'] == selected) == 1
    assert context['最近完整对话'][-1]['用户回答'] == '没有疾病史'
    state = cached(module)
    assert state['qaHistory'][-1]['question']['dialogueFocusRef'] == selected
    assert state['currentQuestion'].get('dialogueFocusRef') is None
    assert selected not in json.dumps(_sanitized_answers(state), ensure_ascii=False)
    assert selected not in json.dumps(family, ensure_ascii=False)
    analysis_requests = [harness.request_payload(row) for row in gateway.requests if row.profile.name == 'record.plan']
    assert selected not in json.dumps(analysis_requests, ensure_ascii=False)
    assert all('对话衔接参考' not in row for row in analysis_requests)


@pytest.mark.asyncio
async def test_invalid_focus_after_repair_keeps_question_without_publishing_metadata():
    manager, module, gateway, _ = setup_manager()

    def choose_unknown(result, payload):
        if payload['responseTask']['action'] == 'ask_intake':
            result['assistantMessage']['focusRef'] = 'dialogue_unknown'

    dialogue_gateway(gateway, edit=choose_unknown)
    first = await manager.createSession('u', 's')
    gateway.next_assessment = assessment(sufficient=False)
    before = len(gateway.requests)
    result = await submit(manager, first, '入睡困难')
    assert len(gateway.requests) == before + 3
    assert result['status'] == 'question' and result['questionType'] == 'checkbox'
    assert result['assistantMessage']
    assert cached(module)['currentQuestion'].get('dialogueFocusRef') is None
    assert 'dialogue_unknown' not in json.dumps(result, ensure_ascii=False)
    assert cached(module)['qaFlow']['generationQuality']['mode'] == 'partial'


def test_dialogue_cohorts_keep_offline_defaults_and_call_budget(tmp_path):
    args = harness.parse_args(['--output-dir', str(tmp_path)])
    assert not args.live and args.max_model_calls == 140
    assert args.scenarios == list(harness.SCENARIOS)
    selected = harness.parse_args(['--output-dir', str(tmp_path), '--scenarios', *harness.DIALOGUE_SCENARIOS])
    assert selected.scenarios == list(harness.DIALOGUE_SCENARIOS)


def test_acceptance_counts_published_questions_instead_of_repair_calls():
    question = {'questionId': 'published-q', 'phase': 'intake', 'intakeDimension': 'sleep'}
    task = {'action': 'ask_intake', 'target': {'dimension': 'sleep', 'scope': 'general'}}
    trace = {
        'scenario': 'single_goal_dialogue',
        'turns': [],
        'decisions': [deepcopy(task), deepcopy(task)],
        'finalState': {
            'qaHistory': [{'question': question}],
            'currentQuestion': deepcopy(question),
        },
    }
    checks = {row['name']: row for row in harness.evaluate_trace(trace)}
    assert checks['question_purposes_not_repeated']['passed']
    assert checks['question_purposes_not_repeated']['purposes'] == [('sleep',)]
    trace['finalState']['currentQuestion']['questionId'] = 'actually-published-again'
    checks = {row['name']: row for row in harness.evaluate_trace(trace)}
    assert not checks['question_purposes_not_repeated']['passed']


def test_new_dialogue_cohorts_run_offline_and_preserve_original_facts(tmp_path, capsys):
    args = SimpleNamespace(
        live=False,
        output_dir=str(tmp_path),
        max_model_calls=140,
        timeout_seconds=30,
        scenarios=list(harness.DIALOGUE_SCENARIOS),
    )
    with patch('aion.create_chat_client', side_effect=AssertionError('offline must never use a provider')):
        assert asyncio.run(harness.acceptance(args, {})) == 0
    capsys.readouterr()
    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert summary['qualityVerdict'] == 'offline_flow_only'
    assert summary['modelLogicalCalls'] <= 140
    assert not summary['productionWrites'] and not summary['externalServices']
    traces = {
        name: json.loads((tmp_path / f'{name}.json').read_text(encoding='utf-8')) for name in harness.DIALOGUE_SCENARIOS
    }
    for trace in traces.values():
        assert all(row['passed'] for row in trace['checks']), trace['checks']
        assert trace['replayUnchanged'] and trace['replayExtraCalls'] == 0
        assert 10 <= trace['finalState']['qaFlow']['intakeQuestionCount'] <= 15
        assert not any('对话衔接参考' in row['request'] for row in trace['calls'] if row['profile'] == 'record.plan')
        assert 'dialogueFocusRef' not in json.dumps(_sanitized_answers(trace['finalState']), ensure_ascii=False)
    assert traces['report_focus_rotation']['finalState']['parsedHealthReports'] == harness.METABOLIC_REPORTS
    report_contexts = [
        row['request']['patient']['对话衔接参考']
        for row in traces['report_focus_rotation']['calls']
        if row['profile'] == 'record.question'
    ]
    assert any(
        {'空腹血糖', '甘油三酯'}.issubset({item['text'] for item in context['可用关注点'] if item['kind'] == 'report'})
        for context in report_contexts
    )
    assert not traces['no_goal_dialogue']['finalState']['qaFlow']['intake']['complaints']
    expanded = traces['free_text_expansion']['finalState']
    assert expanded['qaHistory'][0]['answer'] == harness.OPENING_ANSWER['free_text_expansion']
    assert all(
        (decision.get('target') or {}).get('dimension') not in {'exercise_frequency', 'exercise_duration'}
        for decision in traces['free_text_expansion']['decisions']
    )
    for theme in ('medical_history', 'family_history'):
        decision = next(
            item
            for item in traces['free_text_expansion']['decisions']
            if (item.get('target') or {}).get('dimension') == theme
        )
        assert decision['target']['supplementation']
