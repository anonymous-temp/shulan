"""External multi-survey decoding uses synthetic material and no dependencies."""

from copy import deepcopy

import pytest

from modules.health_analysis.surveys import InvalidSurveyError
from modules.health_analysis.surveys import decode_surveys


def question(name='q', *, kind='radiogroup', title='合成问题', choices=None):
    result = {'name': name, 'type': kind, 'title': title, 'isRequired': True}
    if kind != 'text':
        result['choices'] = choices or [{'value': 'a', 'text': '合成甲'}, {'value': 'b', 'text': '合成乙'}]
    return result


def survey(*questions, answer=None):
    return {
        'schema': {'title': '合成历史问卷', 'pages': [{'name': 'p', 'elements': list(questions or [question()])}]},
        'answer': {} if answer is None else answer,
    }


def test_cross_survey_same_names_use_their_own_labels_and_source_identity():
    data = [
        survey(question(choices=[{'value': 'a', 'text': '每周一至两天'}]), answer={'q': 'a'}),
        survey(question(choices=[{'value': 'a', 'text': '从不摄入'}]), answer={'q': 'a'}),
    ]
    decoded = decode_surveys(data)
    assert decoded.has_historical_schemas and decoded.has_personal_data
    assert [row['readableAnswer'] for row in decoded.personal_answers] == ['每周一至两天', '从不摄入']
    assert [row['surveyIndex'] for row in decoded.asked_questions] == [0, 1]
    assert [row['pointer'] for row in decoded.asked_questions] == [
        '/surveyList/0/schema/pages/0/elements/0',
        '/surveyList/1/schema/pages/0/elements/0',
    ]
    assert [row['status'] for row in decoded.personal_answers] == ['answered', 'answered']


def test_partial_required_answers_keep_all_historical_questions_and_only_actual_material():
    data = [survey(question('missing'), question('null'), question('blank'), question('empty', kind='checkbox'))]
    data[0]['answer'] = {'null': None, 'blank': '   ', 'empty': []}
    decoded = decode_surveys(data)
    assert decoded.has_historical_schemas and not decoded.has_personal_data
    assert len(decoded.asked_questions) == 4
    assert decoded.personal_answers == []
    assert [row['provided'] for row in decoded.asked_questions] == [False, True, True, True]
    assert [row['rawValue'] for row in decoded.asked_questions] == [None, None, '   ', []]
    assert all(row['status'] == 'unknown' and row['value'] is None for row in decoded.asked_questions)


@pytest.mark.parametrize('label', ['不清楚', '不知道', '不确定', '记不清', 'UNKNOWN', 'unsure'])
def test_explicit_unknown_is_actual_personal_material(label):
    decoded = decode_surveys([survey(question(choices=[{'value': 'u', 'text': label}]), answer={'q': 'u'})])
    assert decoded.has_personal_data
    assert decoded.personal_answers[0]['status'] == 'unknown'
    assert decoded.personal_answers[0]['readableAnswer'] == label
    assert decoded.personal_answers[0]['rawValue'] == 'u'


def test_text_preserves_negation_conditions_time_zero_and_negative_results():
    text = '  上周未出现合成不适；运动后才有，数值0，结果阴性。  '
    decoded = decode_surveys([survey(question(kind='text'), answer={'q': text})])
    row = decoded.personal_answers[0]
    assert row['rawValue'] == text
    assert row['readableAnswer'] == text.strip()
    assert row['status'] == 'answered'


def test_real_unknown_text_is_not_converted_to_default_missing():
    decoded = decode_surveys([survey(question(kind='text'), answer={'q': ' 不清楚 '})])
    assert decoded.has_personal_data
    assert decoded.personal_answers[0]['status'] == 'unknown'
    assert decoded.personal_answers[0]['value'] == '不清楚'


def test_multiselect_canonical_order_is_schema_order_while_raw_duplicates_remain_traceable():
    decoded = decode_surveys([survey(question(kind='checkbox'), answer={'q': ['b', 'a', 'b']})])
    row = decoded.personal_answers[0]
    assert row['value'] == ['a', 'b']
    assert row['rawValue'] == ['b', 'a', 'b']
    assert row['readableAnswer'] == '合成甲\n合成乙'


def test_none_is_a_scoped_denial_and_never_rewritten_as_global_health_status():
    item = question(kind='checkbox', title='本周是否出现以下合成情况？')
    item.update(showNoneItem=True, noneText='本题以上均无', noneValue='none')
    decoded = decode_surveys([survey(item, answer={'q': ['none', 'none']})])
    row = decoded.personal_answers[0]
    assert row['status'] == 'denied'
    assert row['value'] == ['none']
    assert row['readableAnswer'] == '本题以上均无'
    assert row['question'] == item
    assert decoded.has_personal_data


def test_none_combined_with_an_ordinary_answer_is_rejected_instead_of_reconciled():
    item = question(kind='checkbox')
    item['showNoneItem'] = True
    with pytest.raises(InvalidSurveyError, match='mutually exclusive'):
        decode_surveys([survey(item, answer={'q': ['none', 'a']})])


def test_question_named_answers_is_an_ordinary_flat_answer_key():
    decoded = decode_surveys([survey(question('answers', kind='text'), answer={'answers': '合成实际回答'})])
    assert decoded.personal_answers[0]['name'] == 'answers'
    assert decoded.personal_answers[0]['readableAnswer'] == '合成实际回答'
    with pytest.raises(InvalidSurveyError):
        decode_surveys([survey(question(), answer={'answers': {'q': 'a'}})])


def test_page_question_locations_and_snapshots_are_retained_without_mutating_input():
    data = [survey(question('first', kind='text'), answer={'first': '  合成原文  '})]
    data[0]['schema']['pages'].append({'name': 'second_page', 'elements': [question('second')]})
    original = deepcopy(data)
    decoded = decode_surveys(data)
    assert data == original
    second = decoded.asked_questions[1]
    assert (second['pageIndex'], second['questionIndex']) == (1, 0)
    assert second['pointer'] == '/surveyList/0/schema/pages/1/elements/0'
    decoded.asked_questions[0]['question']['title'] = '修改返回快照'
    decoded.personal_answers[0]['question']['title'] = '修改另一快照'
    assert data == original


def test_survey_context_retains_time_scope_and_original_page_identity():
    data = survey(question(kind='text'), answer={'q': '合成回答'})
    data['schema']['title'] = '  合成生活习惯资料  '
    data['schema']['description'] = '请仅根据近30天的情况回答，住院期间不计入。'
    data['schema']['pages'][0]['name'] = 'recent_lifestyle'
    decoded = decode_surveys([data])
    for row in [decoded.asked_questions[0], decoded.personal_answers[0]]:
        assert row['surveyTitle'] == data['schema']['title']
        assert row['surveyDescription'] == data['schema']['description']
        assert row['pageName'] == 'recent_lifestyle'
    assert decode_surveys([survey()]).asked_questions[0]['surveyDescription'] is None


@pytest.mark.parametrize('kind', ['text', 'radiogroup', 'checkbox'])
def test_deeply_nested_json_answer_raises_domain_error_before_copying(kind):
    nested = 'synthetic-sensitive-health-sentinel'
    for _ in range(600):
        nested = [nested]
    with pytest.raises(InvalidSurveyError) as error:
        decode_surveys([survey(question(kind=kind), answer={'q': nested})])
    assert 'synthetic-sensitive-health-sentinel' not in str(error.value)


def test_decoded_answer_arrays_do_not_share_input_or_each_other():
    data = survey(question(kind='checkbox'), answer={'q': ['b', 'a', 'b']})
    decoded = decode_surveys([data])
    decoded.asked_questions[0]['rawValue'].append('changed')
    decoded.personal_answers[0]['rawValue'].append('changed_again')
    decoded.personal_answers[0]['value'].append('another_change')
    assert data['answer']['q'] == ['b', 'a', 'b']
    assert decoded.asked_questions[0]['value'] == ['a', 'b']
    assert decoded.personal_answers[0]['rawValue'] == ['b', 'a', 'b', 'changed_again']
    empty = survey(question(kind='checkbox'), answer={'q': []})
    empty_decoded = decode_surveys([empty])
    empty_decoded.asked_questions[0]['rawValue'].append('changed')
    assert empty['answer']['q'] == []


def test_duplicate_names_in_different_pages_of_one_survey_are_rejected():
    data = survey()
    data['schema']['pages'].append({'name': 'another_page', 'elements': [question()]})
    with pytest.raises(InvalidSurveyError, match='duplicate question names'):
        decode_surveys([data])


def test_zero_encoded_as_a_valid_choice_is_personal_material():
    decoded = decode_surveys([survey(question(choices=[{'value': '0', 'text': '0'}]), answer={'q': '0'})])
    assert decoded.has_personal_data
    assert decoded.personal_answers[0]['readableAnswer'] == '0'


@pytest.mark.parametrize('value', [False, 0, 1.2, {}, ['a']])
def test_nontext_values_are_not_silently_classified_as_empty(value):
    with pytest.raises(InvalidSurveyError):
        decode_surveys([survey(question(kind='text'), answer={'q': value})])


@pytest.mark.parametrize(
    'data',
    [
        None,
        {},
        [None],
        [{'schema': {}, 'answer': {}}],
        [{'schema': survey()['schema']}],
        [{**survey(), 'extra': True}],
        [survey(question(kind='radio'))],
        [survey(question(), question())],
        [survey(question(choices=[{'value': 'a'}, {'value': 'a'}]))],
        [survey(question(), answer={'q': 'not_declared'})],
        [survey(question(), answer={'not_declared': 'a'})],
        [survey(question(kind='checkbox'), answer={'q': ['a', 'not_declared']})],
        [survey(question(kind='checkbox'), answer={'q': 'a'})],
    ],
)
def test_invalid_protocol_structure_types_and_values_raise_safe_domain_error(data):
    with pytest.raises(InvalidSurveyError):
        decode_surveys(data)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), (1, 2), {1: '合成值'}])
def test_nonstandard_json_is_rejected_even_inside_unanswered_metadata(value):
    data = [survey()]
    data[0]['schema']['description'] = value
    with pytest.raises(InvalidSurveyError):
        decode_surveys(data)


def test_cyclic_input_is_rejected_with_safe_domain_error():
    data = []
    data.append(data)
    with pytest.raises(InvalidSurveyError):
        decode_surveys(data)


def test_validation_error_does_not_include_question_names_answers_or_health_values():
    sentinel = 'synthetic-sensitive-health-sentinel'
    with pytest.raises(InvalidSurveyError) as error:
        decode_surveys([survey(question(sentinel), answer={sentinel: sentinel})])
    assert sentinel not in str(error.value)
    assert error.value.__cause__ is None


def test_empty_list_has_neither_historical_schemas_nor_personal_material():
    decoded = decode_surveys([])
    assert decoded.asked_questions == decoded.personal_answers == []
    assert not decoded.has_historical_schemas and not decoded.has_personal_data


def test_existing_per_survey_page_question_choice_and_text_limits_are_enforced():
    too_many_pages = survey()
    too_many_pages['schema']['pages'] = [{'name': f'p{i}', 'elements': [question(f'q{i}')]} for i in range(101)]
    too_many_questions = survey(*(question(f'q{i}') for i in range(501)))
    too_many_choices = survey(question(choices=[{'value': str(i)} for i in range(201)]))
    too_long_text = survey(question(kind='text'), answer={'q': 'x' * 20001})
    for data in [too_many_pages, too_many_questions, too_many_choices, too_long_text]:
        with pytest.raises(InvalidSurveyError):
            decode_surveys([data])


def test_multi_survey_decoding_does_not_impose_an_unagreed_cross_survey_limit():
    decoded = decode_surveys([survey() for _ in range(101)])
    assert len(decoded.asked_questions) == 101
    assert decoded.has_historical_schemas and not decoded.has_personal_data
