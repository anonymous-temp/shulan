"""Offline contract tests for the dynamic Record smoke clients."""

from unittest.mock import Mock
from unittest.mock import patch

import pytest

from tests.api.scripts.record_rounds import answer_for
from tests.api.scripts.record_rounds import await_creation
from tests.api.scripts.record_rounds import parse_args
from tests.api.scripts.record_rounds import run_rounds


def response(count, phase='intake', status='question', *, intake=None, qa=0):
    opening = count == 0 and phase == 'intake' and status == 'question'
    return {
        'code': 200,
        'data': {
            'round': 1,
            'status': status,
            'phase': phase,
            'interactionCount': count,
            'intakeQuestionCount': count - qa if intake is None else intake,
            'qaRoundCount': qa,
            'assistantMessage': '承接说明',
            'questionId': f'q-{count}-{phase}',
            'questionType': 'checkbox' if opening else 'text',
            'options': ['肠胃或消化不适', '排便有变化', '体重或精力有变化', '其他情况', '暂无明显不适']
            if opening
            else {'content': ''},
        },
    }


@pytest.mark.parametrize('intake_policy', ['auto', 'legacy'])
@pytest.mark.parametrize('intake_count', [1, 4, 6, 10])
def test_historical_budgets_finish_through_answer_only(intake_count, intake_policy):
    submit = Mock(
        side_effect=[
            *(response(count) for count in range(1, intake_count)),
            response(intake_count, 'supplement'),
            response(intake_count, 'finished', 'finished'),
        ]
    )
    with patch('builtins.print'):
        final = run_rounds(response(0), submit_answer=submit, intake_policy=intake_policy)
    assert final['data']['status'] == 'finished'
    assert submit.call_args.args[1] == '没有了'


@pytest.mark.parametrize('phase', ['supplement', 'qa'])
def test_supplement_batches_and_multiple_concerns_count_once(phase):
    submit = Mock(
        side_effect=[
            response(1),
            response(2),
            response(3),
            response(4, 'supplement'),
            response(5, phase, intake=4, qa=1),
            response(6, phase, intake=4, qa=2),
            response(6, 'finished', 'finished', intake=4, qa=2),
        ]
    )
    with patch('builtins.print'):
        run_rounds(response(0), submit_answer=submit, supplements=['饮食和运动有什么需要关注？', '睡眠呢？'])
    assert [item.args[1] for item in submit.call_args_list[-3:]] == ['饮食和运动有什么需要关注？', '睡眠呢？', '没有了']


@pytest.mark.parametrize('bad', [response(3), response(20), response(1, 'unknown'), {'code': 500}])
def test_invalid_server_output_stops(bad):
    submit = Mock(return_value=bad)
    with patch('builtins.print'), pytest.raises(RuntimeError):
        run_rounds(response(0), submit_answer=submit)


def test_sixth_answer_stops_intake_and_fifth_qa_answer_finishes():
    submit = Mock(
        side_effect=[
            *(response(count) for count in range(1, 6)),
            response(6, 'supplement'),
            *(response(6 + qa, 'qa', intake=6, qa=qa) for qa in range(1, 5)),
            response(11, 'finished', 'finished', intake=6, qa=5),
        ]
    )
    with patch('builtins.print'):
        final = run_rounds(response(0), submit_answer=submit, supplements=['实际问题？'] * 5)
    assert final['data']['qaRoundCount'] == 5
    assert submit.call_count == 11


@pytest.mark.parametrize(
    'responses',
    [
        [response(0, 'supplement')],
        [*(response(count) for count in range(1, 11))],
        [response(1), response(2), response(3), response(4, 'supplement'), response(5, intake=5)],
        [
            response(1),
            response(2),
            response(3),
            response(4, 'supplement'),
            *(response(4 + qa, 'qa', intake=4, qa=qa) for qa in range(1, 6)),
        ],
    ],
)
def test_invalid_intake_and_qa_boundaries_stop(responses):
    submit = Mock(side_effect=responses)
    with patch('builtins.print'), pytest.raises(RuntimeError):
        run_rounds(response(0), submit_answer=submit, supplements=['问题？'] * 6, intake_policy='legacy')


def test_answers_preserve_supported_types():
    assert isinstance(answer_for({'questionType': 'text', 'options': {'symptom': ''}}), dict)
    for kind in ('radio', 'checkbox'):
        assert answer_for({'questionType': kind, 'options': ['甲', '乙']}) == ['甲']


@pytest.mark.parametrize('question_type', ['radio', 'checkbox'])
@pytest.mark.parametrize('option_count', [2, 3, 5])
def test_new_opening_choices_reach_the_answer_endpoint(question_type, option_count):
    first = response(0)
    first['data']['questionType'] = question_type
    first['data']['options'] = first['data']['options'][:option_count]
    submit = Mock(return_value=response(1, 'finished', 'finished'))
    with patch('builtins.print'):
        run_rounds(first, submit_answer=submit)
    assert submit.call_args.args == ('q-0-intake', ['肠胃或消化不适'])


@pytest.mark.parametrize(
    'question',
    [
        {'questionType': 'text', 'options': {'content': ''}},
        {'questionType': 'radio', 'options': ['甲']},
        {'questionType': 'checkbox', 'options': [f'明确项{i}' for i in range(13)]},
        {'questionType': 'checkbox', 'options': ['甲', '乙', '丙', ' ']},
        {'questionType': 'radio', 'options': ['甲', '乙', '丙', ' 甲 ']},
    ],
)
def test_invalid_opening_stops_before_submitting_an_answer(question):
    first = response(0)
    first['data'].update(question)
    submit = Mock()
    with patch('builtins.print'), pytest.raises(RuntimeError, match='新建首题'):
        run_rounds(first, submit_answer=submit)
    submit.assert_not_called()


@pytest.mark.parametrize('option_count', [7, 10, 12])
def test_specific_openings_accept_the_expanded_shortcut_budget(option_count):
    first = response(0)
    first['data'].update(questionType='checkbox', options=[f'明确项{i}' for i in range(option_count)])
    submit = Mock(return_value=response(1, 'finished', 'finished'))
    with patch('builtins.print'):
        run_rounds(first, submit_answer=submit)
    assert submit.call_args.args == ('q-0-intake', ['明确项0'])


def test_supplements_are_opt_in():
    assert parse_args([]).supplement == []
    assert parse_args([]).intake_policy == 'auto'
    assert parse_args(['--intake-policy', 'new']).intake_policy == 'new'
    assert parse_args(['--supplement', '饮食', '--supplement', '运动']).supplement == ['饮食', '运动']
    with patch('sys.stderr'), pytest.raises(SystemExit):
        parse_args(['--continue-rounds', '1'])


@pytest.mark.parametrize('count', [10, 15])
def test_expanded_intake_preserves_all_five_qa_replies(count):
    submit = Mock(
        side_effect=[
            *(response(number) for number in range(1, count)),
            response(count, 'supplement'),
            *(response(count + qa, 'qa', intake=count, qa=qa) for qa in range(1, 5)),
            response(count + 5, 'finished', 'finished', intake=count, qa=5),
        ]
    )
    with patch('builtins.print'):
        final = run_rounds(
            response(0), submit_answer=submit, supplements=['生活安排的一般建议'] * 5, intake_policy='new'
        )
    assert final['data']['qaRoundCount'] == 5
    assert submit.call_count == count + 5


def test_new_policy_allows_genuine_topic_exhaustion_after_the_fixed_themes():
    submit = Mock(
        side_effect=[response(1), response(2), response(3, 'supplement'), response(3, 'finished', 'finished')]
    )
    with patch('builtins.print'):
        result = run_rounds(response(0), submit_answer=submit, intake_policy='new')
    assert result['data']['status'] == 'finished'
    assert submit.call_count == 4


@pytest.mark.parametrize('count', [1, 2])
def test_explicit_new_policy_rejects_invitation_before_three_fixed_themes(count):
    submit = Mock(side_effect=[*(response(n) for n in range(1, count)), response(count, 'supplement')])
    with patch('builtins.print'), pytest.raises(RuntimeError, match='固定主题'):
        run_rounds(response(0), submit_answer=submit, intake_policy='new')


def test_auto_does_not_treat_a_six_question_prefix_as_a_short_policy():
    submit = Mock(
        side_effect=[
            *(response(count) for count in range(1, 15)),
            response(15, 'supplement'),
            response(15, 'finished', 'finished'),
        ]
    )
    with patch('builtins.print'):
        result = run_rounds(response(0), submit_answer=submit, intake_policy='auto')
    assert result['data']['intakeQuestionCount'] == 15


def test_creation_waits_on_same_session_through_processing_and_retryable_failure():
    status = Mock(
        side_effect=[
            {'code': 200, 'data': {'status': 'failed', 'retryable': True, 'retryAfterSeconds': 300}},
            response(0),
        ]
    )
    sleep = Mock()
    result = await_creation({'code': 202, 'data': {'status': 'processing'}}, get_status=status, sleep=sleep)
    assert result == response(0) and status.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [3, 30]


def test_creation_wait_stops_on_terminal_failure_or_deadline():
    status = Mock()
    with pytest.raises(RuntimeError, match='停止恢复'):
        await_creation({'code': 409, 'data': {'status': 'failed', 'retryable': False}}, get_status=status)
    with pytest.raises(RuntimeError, match='超时'):
        await_creation({'code': 202, 'data': {'status': 'processing'}}, get_status=status, timeout=0)
    status.assert_not_called()
