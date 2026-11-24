"""Synthetic stage contracts exercise the actual generation and publication path."""

import asyncio
from copy import deepcopy

import pytest

from modules.health_analysis.contracts import AnalysisGenerationFailure
from modules.health_analysis.contracts import AnalysisGenerationStage
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.deduplication import normalized_title
from modules.health_analysis.factors import HealthAnalysisGenerationError
from modules.health_analysis.generation import HealthAnalysisGenerator
from modules.health_analysis.policy import DEFAULT_ORDER
from modules.health_analysis.policy import LIFESTYLE_TARGETS
from modules.health_analysis.policy import background_questions
from modules.health_analysis.policy import build_schema
from modules.health_analysis.policy import has_unknown_option
from modules.health_analysis.policy import template_question
from modules.health_analysis.surveys import decode_surveys
from tests.unit._support.health_analysis import StageModel


def snapshot(*, materials=None, history=False, gender=None):
    asked = (
        decode_surveys(
            [
                {
                    'schema': {
                        'title': '合成旧卷',
                        'pages': [{'name': 'p', 'elements': [template_question('exercise_frequency')]}],
                    },
                    'answer': {},
                }
            ]
        ).asked_questions
        if history
        else []
    )
    return {
        'personal_materials': materials or [],
        'asked_questions': asked,
        'has_personal_data': bool(materials),
        'has_historical_schemas': history,
        'profile_gender': gender,
        'effective_demographics': {'ageYears': 42, 'gender': gender, 'sourceReliability': 'very_high'},
    }


def material(kind='answer', content='每周活动一至两天', status='answered'):
    return {'id': 'm1', 'kind': kind, 'content': content, 'status': status}


def questions(output):
    schema = output['result']['advancedSurveySchema']
    return [] if schema is None else [q for page in schema['pages'] for q in page['elements']]


def history_answer(question, value, *, schema=None):
    decoded = decode_surveys(
        [
            {
                'schema': schema
                if schema is not None
                else {'title': '合成问卷', 'pages': [{'name': 'p', 'elements': [question]}]},
                'answer': {question['name']: value},
            }
        ]
    )
    (row,) = decoded.personal_answers
    return decoded.asked_questions, material('answer', row, row['status'])


def background_names(output):
    return [q['name'] for q in questions(output) if not q['name'].startswith('ha_')]


def report_fact(fact_type, observation, *, status='active', verification='verified_source'):
    return {
        **material('report', {'factType': fact_type, 'observation': observation}),
        'status': 'answered' if status == 'active' else 'unknown',
        'metadata': {'factStatus': status, 'verificationStatus': verification},
    }


@pytest.mark.asyncio
async def test_no_materials_or_history_has_three_topics_and_ten_lifestyle_questions_without_personal_inference():
    model = StageModel()
    output = await HealthAnalysisGenerator(model).generate(snapshot(gender='male'))
    assert len(questions(output)) == 13
    assert questions(output)[:3] == background_questions()
    assert all(output['result'][field] == '' for field in ('healthReview', 'suggestedMaterials', 'advancedQuestions'))
    assert not model.calls
    assert not any(has_unknown_option(template_question(key)) for key in LIFESTYLE_TARGETS)
    assert not any(has_unknown_option(question) for question in questions(output)[3:])
    activity = next(question for question in questions(output) if question['name'] == 'activity')
    assert activity['showNoneItem'] and activity['noneValue'] == 'none'
    assert output['trace']['effectiveHasPersonalData'] is False
    assert output['trace']['execution']['capability'] == 'health_analysis.main'
    decode_surveys([{'schema': output['result']['advancedSurveySchema'], 'answer': {}}])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('target_id', 'title', 'description', 'options'),
    [
        (
            'sleep',
            '您平时通常几点开始准备睡觉？',
            None,
            [
                ('1', '晚上22点前'),
                ('2', '22点到次日0点前'),
                ('3', '次日0点及以后'),
                ('4', '时间不固定'),
            ],
        ),
        (
            'sleep_duration',
            '您最近平均每天实际睡了多久？',
            '包括午睡等白天睡着的时间，只计算实际睡着的时间',
            [
                ('1', '不足6小时'),
                ('2', '6至不足7小时'),
                ('3', '7至不足9小时'),
                ('4', '9小时及以上'),
            ],
        ),
        (
            'rest_schedule',
            '在工作、学习或家务之外，您平时通常怎样留时间休息或放松？',
            None,
            [
                ('fixed_regular_days', '平日也有固定的休息时间'),
                ('fixed_days_off', '只有休息日才固定安排'),
                ('3', '没有固定时间，有空时休息'),
                ('4', '通常不专门留时间休息'),
            ],
        ),
        (
            'diet_structure',
            '您平时的正餐通常包括哪些类食物？可多选。',
            None,
            [
                ('staple', '谷薯类主食'),
                ('4', '蔬菜类'),
                ('5', '水果类'),
                ('meat_poultry', '畜禽肉类'),
                ('aquatic_foods', '水产类'),
                ('eggs', '蛋类'),
                ('beans_soy_products', '豆类及豆制品'),
            ],
        ),
    ],
)
async def test_basic_questionnaire_uses_the_revised_lifestyle_templates(target_id, title, description, options):
    result = await HealthAnalysisGenerator(StageModel()).generate(snapshot())
    question = next(row for row in questions(result) if row['name'] == target_id)

    assert question['title'] == title
    if description is not None:
        assert question['description'].rstrip('。') == description
    assert question['type'] == ('checkbox' if target_id == 'diet_structure' else 'radiogroup')
    assert question['choices'] == [{'value': value, 'text': text} for value, text in options]


@pytest.mark.asyncio
async def test_unanswered_history_repeats_missing_topics_and_still_excludes_old_lifestyle_questions():
    model = StageModel(states={'exercise_frequency': 'history_covered'})
    data = snapshot(history=True)
    data['asked_questions'].append(
        {**data['asked_questions'][0], 'pointer': '/history/1', 'question': background_questions()[0]}
    )
    output = await HealthAnalysisGenerator(model).generate(data)
    assert len(questions(output)) == 20
    assert questions(output)[:3] == background_questions()
    assert 'ha_exercise_frequency' not in {q['name'] for q in questions(output)}
    assert all(q['name'].removeprefix('ha_') in LIFESTYLE_TARGETS for q in questions(output)[3:])
    assert {stage for stage, _ in model.calls} == {'coverage', 'review'}
    (review,) = [body for stage, body in model.calls if stage == 'review']
    assert review['survey']['questionIds'] == [row['candidateId'] for row in review['questions']]
    assert [row['question'] for row in review['questions']] == questions(output)
    assert output['trace']['surveyProtocolRecoveries'] == []
    assert all(output['result'][field] == '' for field in ('healthReview', 'suggestedMaterials', 'advancedQuestions'))


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [7, 1, 0])
async def test_seven_one_and_zero_remaining_targets_have_complete_coverage_without_invented_questions(count):
    states = {key: 'history_covered' for key in DEFAULT_ORDER[count:]}
    output = await HealthAnalysisGenerator(StageModel(states=states)).generate(snapshot(history=True))
    assert len(questions(output)) == count + 3
    assert questions(output)[:3] == background_questions()
    assert len(output['trace']['coverage']) == 24


@pytest.mark.asyncio
@pytest.mark.parametrize('target_id, value', [('health_goals', '9'), ('medical_history', '8'), ('family_history', '9')])
async def test_explicit_unknown_answer_closes_only_its_topic_without_generating_clinical_content(target_id, value):
    initial = await HealthAnalysisGenerator(StageModel()).generate(snapshot())
    question = next(q for q in questions(initial) if q['name'] == target_id)
    assert {'value': value, 'text': '不清楚'} in question['choices']
    asked, answer = history_answer(question, [value])
    assert answer['status'] == 'unknown'
    model = StageModel()
    data = snapshot(materials=[answer], history=True)
    data['asked_questions'] = asked
    output = await HealthAnalysisGenerator(model).generate(data)
    assert background_names(output) == [q['name'] for q in background_questions() if q['name'] != target_id]
    assert len(questions(output)) <= 20
    assert output['trace']['effectiveHasPersonalData'] is False
    assert all(output['result'][field] == '' for field in ('healthReview', 'suggestedMaterials', 'advancedQuestions'))
    assert {stage for stage, _ in model.calls} == {'coverage', 'review'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mislabelled_theme', [None, 'family_history'])
@pytest.mark.parametrize('includes_unknown_option', [True, False])
async def test_canonical_answer_covers_its_topic_even_if_the_model_omits_or_mislabels_it(
    mislabelled_theme, includes_unknown_option
):
    question = background_questions()[1]
    if not includes_unknown_option:
        question['choices'].pop()
    asked, answer = history_answer(question, ['none'])
    evidence = [{'evidenceId': 'm1', 'quote': answer['content']['readableAnswer']}]
    model = StageModel(background={mislabelled_theme: evidence} if mislabelled_theme else {})
    data = snapshot(materials=[answer], history=True)
    data['asked_questions'] = asked
    output = await HealthAnalysisGenerator(model).generate(data)
    assert background_names(output) == ['health_goals', 'family_history']


@pytest.mark.asyncio
async def test_lifestyle_answer_to_known_template_cannot_cover_disease_history():
    question = template_question('exercise_frequency')
    asked, answer = history_answer(question, question['choices'][0]['value'])
    model = StageModel(
        background={'medical_history': [{'evidenceId': 'm1', 'quote': answer['content']['readableAnswer']}]}
    )
    data = snapshot(materials=[answer], history=True)
    data['asked_questions'] = asked
    output = await HealthAnalysisGenerator(model).generate(data)
    assert questions(output)[:3] == background_questions()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('target_id', 'title', 'question_type', 'labels', 'chosen', 'decoded_label', 'status'),
    [
        (
            'sleep',
            '您平时通常在什么时间准备睡觉？',
            'radiogroup',
            ['22点前', '22点至午夜前', '午夜及以后', '时间不固定'],
            '2',
            '22点至午夜前',
            'answered',
        ),
        (
            'sleep_duration',
            '您最近通常每天睡多长时间？',
            'radiogroup',
            ['不足6小时', '6至7小时', '7至9小时', '9小时及以上', '不清楚'],
            '5',
            '不清楚',
            'unknown',
        ),
        (
            'rest_schedule',
            '日常生活中，您通常怎样安排属于自己的休息时间？',
            'radiogroup',
            ['每天固定休息', '休息日休息', '有空时休息', '没有专门休息'],
            '2',
            '休息日休息',
            'answered',
        ),
        (
            'diet_structure',
            '您平时的正餐通常吃到哪些食物？可多选。',
            'checkbox',
            ['米饭', '面条', '杂粮', '蔬菜', '水果', '猪肉', '鱼肉', '鸡蛋', '豆制品'],
            ['6'],
            '猪肉',
            'answered',
        ),
    ],
)
async def test_legacy_lifestyle_templates_keep_their_answers_and_cover_the_same_target(
    target_id, title, question_type, labels, chosen, decoded_label, status
):
    question = {
        'name': f'ha_{target_id}',
        'type': question_type,
        'title': title,
        'choices': [{'value': str(index), 'text': label} for index, label in enumerate(labels, 1)],
    }
    asked, answer = history_answer(question, chosen, schema=build_schema([question]))
    assert answer['content']['readableAnswer'] == decoded_label
    assert answer['status'] == status

    # Deliberately have the model mark the only unresolved goal as still askable
    # and try to reuse the lifestyle answer as a basic medical-history answer.
    model = StageModel(
        states={key: 'history_covered' for key in DEFAULT_ORDER if key != target_id},
        background={'medical_history': [{'evidenceId': answer['id'], 'quote': decoded_label}]},
    )
    data = snapshot(materials=[answer], history=True)
    data['asked_questions'] = asked
    output = await HealthAnalysisGenerator(model).generate(data)

    coverage = next(row for row in output['trace']['coverage'] if row['targetId'] == target_id)
    assert coverage['status'] == 'history_covered'
    assert coverage['historyRefs'] == [asked[0]['pointer']]
    assert questions(output) == background_questions()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed_field', ['survey_description', 'question_description', 'options'])
async def test_legacy_title_does_not_cover_a_differently_scoped_question(changed_field):
    target_id = 'sleep_duration'
    old_question = {
        'name': 'ha_sleep_duration',
        'type': 'radiogroup',
        'title': '您最近通常每天睡多长时间？',
        'choices': [
            {'value': str(index), 'text': label}
            for index, label in enumerate(['不足6小时', '6至7小时', '7至9小时', '9小时及以上', '不清楚'], start=1)
        ],
    }
    if changed_field == 'question_description':
        old_question['description'] = '只统计此次住院期间的夜间睡眠。'
    if changed_field == 'options':
        old_question['choices'] = [{'value': '1', 'text': '不足8小时'}, {'value': '2', 'text': '8小时及以上'}]
    schema = build_schema([old_question])
    if changed_field == 'survey_description':
        schema['description'] = '本问卷只涉及住院期间的作息。'
    other_questions = [template_question(key) for key in DEFAULT_ORDER if key != target_id]
    decoded = decode_surveys(
        [
            {'schema': build_schema(other_questions[:12]), 'answer': {}},
            {'schema': build_schema(other_questions[12:]), 'answer': {}},
            {'schema': schema, 'answer': {}},
        ]
    )
    data = snapshot(history=True)
    data['asked_questions'] = decoded.asked_questions

    output = await HealthAnalysisGenerator(StageModel()).generate(data)

    coverage = next(row for row in output['trace']['coverage'] if row['targetId'] == target_id)
    assert coverage['status'] == 'askable'
    assert coverage['historyRefs'] == []
    assert questions(output) == background_questions() + [{**template_question(target_id), 'name': 'ha_sleep_duration'}]


@pytest.mark.asyncio
async def test_combined_report_chat_and_history_fill_separate_topics_and_can_exhaust_full_questionnaire():
    asked, denial = history_answer(background_questions()[0], ['none'])
    diagnosis = {**report_fact('condition.diagnosis', '既往确诊高血压'), 'id': 'm2'}
    family = {**material('chat', '我父亲有糖尿病'), 'id': 'm3'}
    model = StageModel(
        states=dict.fromkeys(DEFAULT_ORDER, 'history_covered'),
        classification='family_history',
        background={
            'health_goals': [{'evidenceId': 'm1', 'quote': denial['content']['readableAnswer']}],
            'medical_history': [{'evidenceId': 'm2', 'quote': '既往确诊高血压'}],
            'family_history': [{'evidenceId': 'm3', 'quote': family['content']}],
        },
    )
    data = snapshot(materials=[denial, diagnosis, family], history=True)
    data['asked_questions'] = asked
    output = await HealthAnalysisGenerator(model).generate(data)
    assert output['result']['advancedSurveySchema'] is None
    assert output['result']['healthReview']
    plan = next(body for stage, body in model.calls if stage == 'plan')
    family_context = next(row for row in plan['materials'] if row['id'] == 'm3')
    assert family_context['backgroundScope'] == 'family_history'
    targets = next(body for stage, body in model.calls if stage == 'targets')
    assert 'm3' not in {row['id'] for row in targets['materials']}


@pytest.mark.asyncio
async def test_report_and_chat_can_cover_topics_without_any_historical_survey():
    diagnosis = report_fact('condition.diagnosis', '有高血压病史')
    goal = {**report_fact('goal.health', '本次希望改善睡眠'), 'id': 'm2'}
    family = {**material('chat', '我不清楚父亲有没有慢性病'), 'id': 'm3'}
    model = StageModel(
        classification='family_history_unknown',
        background={
            'health_goals': [{'evidenceId': 'm2', 'quote': '本次希望改善睡眠'}],
            'medical_history': [{'evidenceId': 'm1', 'quote': '有高血压病史'}],
            'family_history': [{'evidenceId': 'm3', 'quote': family['content']}],
        },
    )
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[diagnosis, goal, family]))
    assert len(questions(output)) == 20
    assert not background_names(output)


@pytest.mark.asyncio
async def test_conflicting_in_scope_answers_still_close_the_answered_theme():
    asked, denial = history_answer(background_questions()[1], ['none'])
    report = {**report_fact('condition.diagnosis', '曾确诊高血压'), 'id': 'm2'}
    model = StageModel(
        background={
            'medical_history': [
                {'evidenceId': 'm1', 'quote': denial['content']['readableAnswer']},
                {'evidenceId': 'm2', 'quote': '曾确诊高血压'},
            ],
        },
    )
    data = snapshot(materials=[denial, report], history=True)
    data['asked_questions'] = asked
    output = await HealthAnalysisGenerator(model).generate(data)
    assert background_names(output) == ['health_goals', 'family_history']
    plan = next(body for stage, body in model.calls if stage == 'plan')
    assert {row['id'] for row in plan['materials']} == {'m1', 'm2'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'row,quote,classification',
    [
        (report_fact('lab.glucose', 8.1), '8.1', 'self_statement'),
        (
            report_fact('condition.diagnosis', '高血压', status='pending', verification='unconfirmed'),
            '高血压',
            'self_statement',
        ),
        (report_fact('condition.diagnosis', '没有高血压'), '高血压', 'self_statement'),
        (material('chat', '同事有高血压'), '同事有高血压', 'other_subject'),
    ],
)
async def test_insufficient_evidence_cannot_suppress_a_basic_topic(row, quote, classification):
    model = StageModel(
        classification=classification,
        background={'medical_history': [{'evidenceId': 'm1', 'quote': quote}]},
    )
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[deepcopy(row)], history=True))
    assert questions(output)[:3] == background_questions()
    if row['kind'] == 'chat' or row.get('metadata', {}).get('factStatus') == 'pending':
        assert all(
            output['result'][field] == '' for field in ('healthReview', 'suggestedMaterials', 'advancedQuestions')
        )


@pytest.mark.asyncio
async def test_initial_report_with_no_eligible_lifestyle_questions_still_publishes_three_topics():
    model = StageModel(states=dict.fromkeys(DEFAULT_ORDER, 'material_covered'))
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material('report')]))
    assert questions(output) == background_questions()
    assert output['trace']['selectedQuestionCount'] == 3


@pytest.mark.asyncio
async def test_personalized_lifestyle_candidates_cannot_repeat_a_fixed_topic():
    report = material('report')
    model = StageModel()
    plan = (await model.run('plan', {'materials': [report]}))['output']
    plan['lifestyleOrder'] = ['sleep_duration']
    plan['questions'] = [{'targetId': 'diet_structure', 'question': background_questions()[0]}]
    model.plan = plan
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[report]))
    result = questions(output)
    assert result[:3] == background_questions()
    assert result[3]['name'] == 'ha_sleep_duration'
    assert len({normalized_title(question['title']) for question in result}) == len(result)
    assert any(row['reason'] == 'exact_new_duplicate' for row in output['trace']['filters'])


@pytest.mark.asyncio
@pytest.mark.parametrize('classification', ['question', 'other_subject', 'nonclinical', 'uncertain'])
async def test_patient_sender_chat_is_not_automatically_a_personal_health_fact(classification):
    model = StageModel(classification=classification)
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material('chat', '合成疾病是什么？')]))
    assert not output['trace']['effectiveHasPersonalData']
    assert len(questions(output)) == 13
    assert output['result']['healthReview'] == ''
    assert [stage for stage, _ in model.calls] == ['materials']
    assert output['trace']['materialDecisions'][0]['quote'] == '合成疾病是什么？'


@pytest.mark.asyncio
async def test_stated_lifestyle_uncertainty_remains_context_and_never_fills_the_basic_topics():
    model = StageModel(classification='self_unknown')
    output = await HealthAnalysisGenerator(model).generate(
        snapshot(materials=[material('chat', '我记不清最近的活动情况')])
    )
    assert output['trace']['effectiveHasPersonalData'] is False
    assert questions(output)[:3] == background_questions()
    assert all(output['result'][field] == '' for field in ('healthReview', 'suggestedMaterials', 'advancedQuestions'))
    assert {stage for stage, _ in model.calls} == {'materials', 'coverage'}


@pytest.mark.asyncio
async def test_three_background_and_seven_lifestyle_questions_can_publish_with_unselected_uncertainty():
    states = {key: 'uncertain' for key in DEFAULT_ORDER[7:]}
    model = StageModel(states=states)
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material('report')]))
    assert len(questions(output)) == 10
    assert questions(output)[:3] == background_questions()
    assert sum(row['status'] == 'uncertain' for row in output['trace']['coverage']) == 17


@pytest.mark.asyncio
async def test_invalid_candidate_does_not_close_an_askable_target_or_create_false_exhaustion():
    model = StageModel(rejected=set(DEFAULT_ORDER))
    with pytest.raises(HealthAnalysisGenerationError, match='complete catalogue coverage'):
        await HealthAnalysisGenerator(model).generate(snapshot(history=True))
    assert sum(stage == 'review' for stage, _ in model.calls) == 2
    assert sum(stage == 'coverage' for stage, _ in model.calls) == 2


@pytest.mark.asyncio
async def test_backfill_prioritizes_unreviewed_targets_over_rejected_templates():
    # The history fixture already contains exercise_frequency. Of the 17
    # lifestyle questions in the first draft, only six will pass review.
    eligible = [key for key in DEFAULT_ORDER if key != 'exercise_frequency']
    model = StageModel(rejected=set(eligible[6:17]))

    output = await HealthAnalysisGenerator(model).generate(snapshot(history=True))

    initial, backfill = [body for stage, body in model.calls if stage == 'review']
    assert len(initial['survey']['questionIds']) == len(backfill['survey']['questionIds']) == 20
    backfill_targets = [row['targetId'] for row in backfill['questions']]
    assert backfill_targets[:6] == eligible[17:]
    assert [q['name'] for q in questions(output)[3:]] == [f'ha_{key}' for key in eligible[:6] + eligible[17:]]


@pytest.mark.asyncio
@pytest.mark.parametrize('multiple_backfill_batches', [False, True])
async def test_backfill_reviews_new_templates_before_giving_up_on_askable_targets(multiple_backfill_batches):
    eligible = [key for key in DEFAULT_ORDER if key != 'exercise_frequency']
    usable_target = eligible[-1] if multiple_backfill_batches else eligible[16]
    planned_targets = eligible[6:] if multiple_backfill_batches else [usable_target]
    draft_description = '请以最近一周的实际情况回答。'

    class BackfillModel(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            output = response['output']
            if stage == 'plan':
                output['questions'] = [
                    {'targetId': key, 'question': {**template_question(key), 'description': draft_description}}
                    for key in planned_targets
                ]
            if stage == 'review':
                for candidate, decision in zip(payload['questions'], output['questions'], strict=True):
                    if candidate['targetId'] == usable_target and candidate['question'] == {
                        **template_question(usable_target),
                        'name': f'ha_{usable_target}',
                    }:
                        decision['verdict'] = 'approved'
            return response

    model = BackfillModel(rejected=set(eligible[6:]))
    output = await HealthAnalysisGenerator(model).generate(snapshot(history=True, materials=[material()]))

    assert len(questions(output)) == 10
    assert questions(output)[-1]['name'] == f'ha_{usable_target}'
    reviews = [body for stage, body in model.calls if stage == 'review']
    assert len(reviews) == (4 if multiple_backfill_batches else 2)
    assert all(len(review['survey']['questionIds']) <= 20 for review in reviews)
    assert sum(stage == 'coverage' for stage, _ in model.calls) == 2


@pytest.mark.asyncio
async def test_backfill_reviews_candidate_after_conflicting_draft_is_rejected():
    targets = ('sleep', 'sleep_duration')
    eligible = {key for key in DEFAULT_ORDER if key not in targets}
    approved = {key for key in DEFAULT_ORDER[:8] if key in eligible}
    title = '最近一周，您平均每天实际睡着多久？'

    class DuplicateDraftModel(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            output = response['output']
            if stage == 'plan':
                output['questions'] = [
                    {'targetId': key, 'question': {**template_question(key), 'title': title}} for key in targets
                ]
            if stage == 'review':
                for candidate, decision in zip(payload['questions'], output['questions'], strict=True):
                    if candidate['targetId'] == 'sleep_duration' and candidate['question']['title'] == title:
                        decision['verdict'] = 'approved'
            return response

    states = {key: 'material_covered' for key in eligible - approved}
    model = DuplicateDraftModel(states=states, rejected=set(targets))
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    first, backfill = [body for stage, body in model.calls if stage == 'review']
    assert [row['targetId'] for row in first['questions'] if row['question']['title'] == title] == ['sleep']
    assert [row['targetId'] for row in backfill['questions'] if row['question']['title'] == title] == ['sleep_duration']
    assert len(questions(output)) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('different_scope', [False, True])
async def test_canonical_history_settles_goal_when_coverage_model_misses_it(different_scope):
    other_targets = [template_question(key) for key in DEFAULT_ORDER if key != 'smoking']
    smoking_schema = build_schema([template_question('smoking')])
    if different_scope:
        smoking_schema['description'] = '以下问题仅询问您的父亲。'
    decoded = decode_surveys(
        [
            {'schema': build_schema(other_targets[:12]), 'answer': {}},
            {'schema': build_schema(other_targets[12:]), 'answer': {}},
            {'schema': smoking_schema, 'answer': {}},
        ]
    )
    model = StageModel(states={key: 'history_covered' for key in DEFAULT_ORDER if key != 'smoking'})
    data = snapshot(history=True)
    data['asked_questions'] = decoded.asked_questions

    if different_scope:
        with pytest.raises(HealthAnalysisGenerationError) as failure:
            await HealthAnalysisGenerator(model).generate(data)
        assert failure.value.reason is AnalysisGenerationFailure.SURVEY_COVERAGE_INCOMPLETE
        return

    output = await HealthAnalysisGenerator(model).generate(data)
    assert questions(output) == background_questions()
    smoking = next(row for row in output['trace']['coverage'] if row['targetId'] == 'smoking')
    assert smoking['status'] == 'history_covered'
    assert smoking['historyRefs'] == [decoded.asked_questions[-1]['pointer']]


@pytest.mark.asyncio
async def test_single_rejected_candidate_keeps_other_qualified_questions():
    output = await HealthAnalysisGenerator(StageModel(rejected={'diet_structure'})).generate(snapshot(history=True))
    assert len(questions(output)) == 19
    target = next(row for row in output['trace']['coverage'] if row['targetId'] == 'diet_structure')
    assert target['status'] == 'askable'
    assert any(row.get('targetId') == 'diet_structure' for row in output['trace']['filters'])


@pytest.mark.asyncio
async def test_coverage_reassessment_keeps_the_status_of_an_accepted_question():
    class CoverageReassessmentModel(StageModel):
        async def run(self, stage, payload):
            if stage == 'coverage' and payload['feedback']:
                self.states.update(diet_structure='material_covered', sleep_duration='material_covered')
            return await super().run(stage, payload)

    model = CoverageReassessmentModel(
        states={key: 'material_covered' for key in DEFAULT_ORDER if key not in {'diet_structure', 'sleep_duration'}},
        rejected={'sleep_duration'},
    )
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    assert [q['name'] for q in questions(output) if q['name'].startswith('ha_')] == ['ha_diet_structure']
    coverage = {row['targetId']: row['status'] for row in output['trace']['coverage']}
    assert coverage['diet_structure'] == 'askable'
    assert coverage['sleep_duration'] == 'material_covered'
    initial, reassessment = [payload for stage, payload in model.calls if stage == 'coverage']
    assert not initial['feedback']
    assert any(row['targetId'] == 'sleep_duration' for row in reassessment['feedback'])
    assert sum(stage == 'review' for stage, _ in model.calls) == 1


class SurveyRewriteModel(StageModel):
    def __init__(self, replacements=(), *, finding=None, proposal=None, repeat=False, target_id='diet_structure'):
        super().__init__()
        self.replacements = iter(replacements)
        self.finding = deepcopy(finding)
        self.proposal = proposal
        self.repeat = repeat
        self.target_id = target_id

    async def run(self, stage, payload):
        if stage == 'survey_rewrite':
            self.calls.append((stage, deepcopy(payload)))
            assert len(payload['questions']) == len(payload['feedback']) == 1
            (row,) = payload['questions']
            return {
                'output': {'questions': [{'candidateId': row['candidateId'], 'question': next(self.replacements)}]},
                'model_identity': {'provider': 'offline', 'model': 'synthetic-v1'},
            }
        response = await super().run(stage, payload)
        if stage == 'review' and payload.get('survey') and self.finding:
            for verdict in response['output']['questions']:
                if verdict['candidateId'] == f'q0_{self.target_id}':
                    verdict.update(verdict='requires_revision', issues=[self.finding])
                    if self.proposal is not None:
                        verdict['replacement'] = self.proposal
            if not self.repeat:
                self.finding = None
        return response


async def plan_with_lifestyle_question(model, question, *, target_id='diet_structure'):
    plan = (await model.run('plan', {'materials': [material()]}))['output']
    plan['questions'] = [{'targetId': target_id, 'question': question}]
    model.plan = plan


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('target_id', 'planned_type'),
    [
        ('sleep', 'checkbox'),
        ('sleep_duration', 'checkbox'),
        ('rest_schedule', 'checkbox'),
        ('diet_structure', 'radiogroup'),
    ],
)
async def test_wrong_type_for_fixed_target_uses_catalogue_question(target_id, planned_type):
    template = template_question(target_id)
    model = StageModel()
    await plan_with_lifestyle_question(model, {**template, 'type': planned_type}, target_id=target_id)

    result = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    (review,) = [body for stage, body in model.calls if stage == 'review']
    reviewed = next(row['question'] for row in review['questions'] if row['targetId'] == target_id)
    assert reviewed == {**template, 'name': f'ha_{target_id}'}
    assert reviewed in questions(result)
    assert {'targetId': target_id, 'reason': 'invalid_question'} in result['trace']['filters']


@pytest.mark.asyncio
async def test_inline_repair_uses_one_targeted_review_without_a_separate_rewrite_call():
    original = template_question('diet_structure')
    bad_choices = [{'value': 'rice', 'text': '米饭'}, {'value': 'vegetables', 'text': '蔬菜类'}]
    correct_choices = original['choices']
    original['choices'] = bad_choices
    finding = {'ruleId': 'diet_option_categories', 'field': 'choices', 'reason': '具体食物和食物大类混在了一起。'}
    model = SurveyRewriteModel(finding=finding, proposal={'choices': correct_choices})
    await plan_with_lifestyle_question(model, original)

    result = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    reviews = [body for stage, body in model.calls if stage == 'review']
    assert len(reviews) == 2
    assert not any(stage == 'survey_rewrite' for stage, _ in model.calls)
    assert len(reviews[0]['survey']['questionIds']) == 20

    planned = next(body for stage, body in model.calls if stage == 'plan' and 'surveyRules' in body)
    assert reviews[0]['survey']['rules'] == planned['surveyRules']
    scoped = {rule['ruleId']: rule['targetIds'] for rule in planned['surveyRules'] if 'targetIds' in rule}
    assert scoped == {
        'bedtime_clock': ['sleep'],
        'sleep_duration_scope': ['sleep_duration'],
        'rest_schedule_scene': ['rest_schedule'],
        'diet_option_categories': ['diet_structure'],
    }
    assert reviews[1]['survey']['draftVersion'] == reviews[0]['survey']['draftVersion'] + 1
    (candidate,) = reviews[1]['questions']
    assert candidate['targetId'] == 'diet_structure'
    assert candidate['question']['choices'] == correct_choices
    assert len(reviews[1]['retainedQuestions']) == 19
    binding = next(row for row in result['trace']['questionBindings'] if row.get('targetId') == 'diet_structure')
    assert binding['question']['choices'] == correct_choices
    first_review = result['trace']['surveyQualityReviews'][0]
    assert first_review['findings'] == [{'candidateId': candidate['candidateId'], 'issues': [finding]}]
    (edit,) = first_review['revisions']
    assert edit['changes']['choices'] == {'from': bad_choices, 'to': correct_choices}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('target_id', 'unknown_label', 'true_label'),
    [('sleep', '不 清 楚', '时间不固定'), ('exercise_frequency', '记不清', '没有运动')],
)
async def test_complete_catalogue_choices_drop_unknown_before_one_review(target_id, unknown_label, true_label):
    candidate = template_question(target_id)
    expected = [*candidate['choices']]
    candidate['choices'].append({'value': 'unknown', 'text': unknown_label})
    model = StageModel()
    await plan_with_lifestyle_question(model, candidate, target_id=target_id)

    result = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    (review,) = [body for stage, body in model.calls if stage == 'review']
    reviewed = next(row['question'] for row in review['questions'] if row['targetId'] == target_id)
    selected = next(row for row in questions(result) if row['name'] == f'ha_{target_id}')
    assert reviewed['choices'] == selected['choices'] == expected
    assert true_label in [choice['text'] for choice in selected['choices']]
    assert not any(stage == 'survey_rewrite' for stage, _ in model.calls)


@pytest.mark.asyncio
async def test_unknown_option_is_not_stripped_from_an_incomplete_dynamic_choice_set():
    candidate = template_question('sleep_duration')
    candidate['choices'].pop(0)
    candidate['choices'].append({'value': 'unknown', 'text': '不清楚'})
    model = StageModel()
    await plan_with_lifestyle_question(model, candidate, target_id='sleep_duration')

    await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    (review,) = [body for stage, body in model.calls if stage == 'review']
    reviewed = next(row['question'] for row in review['questions'] if row['targetId'] == 'sleep_duration')
    assert reviewed['choices'] == candidate['choices']


@pytest.mark.asyncio
async def test_joint_review_removes_unknown_when_noncanonical_choices_cover_the_answer_range():
    candidate = template_question('sleep_duration')
    complete_choices = [
        {'value': 'under7', 'text': '不足7小时'},
        {'value': '7to9', 'text': '7至不足9小时'},
        {'value': '9plus', 'text': '9小时及以上'},
    ]
    candidate['choices'] = [*complete_choices, {'value': 'unknown', 'text': '不清楚'}]
    finding = {'ruleId': 'unknown_option', 'field': 'choices', 'reason': '具体的时长区间已覆盖实际答案。'}
    model = SurveyRewriteModel(
        finding=finding,
        proposal={'choices': complete_choices},
        target_id='sleep_duration',
    )
    await plan_with_lifestyle_question(model, candidate, target_id='sleep_duration')

    result = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))

    first, second = [body for stage, body in model.calls if stage == 'review']
    first_question = next(row['question'] for row in first['questions'] if row['targetId'] == 'sleep_duration')
    assert has_unknown_option(first_question)
    (corrected,) = second['questions']
    assert corrected['targetId'] == 'sleep_duration'
    assert corrected['question']['choices'] == complete_choices
    assert corrected['question'] in questions(result)
    assert not any(stage == 'survey_rewrite' for stage, _ in model.calls)


@pytest.mark.asyncio
async def test_rewrite_of_a_historical_duplicate_is_checked_before_reviewing_the_new_text():
    original = template_question('diet_structure')
    original['title'] = '您通常如何搭配一餐？'
    corrected = {**original, 'title': '您一顿正餐一般吃哪些食物？'}
    data = snapshot(materials=[material()], history=True)
    duplicate = {**original, 'title': data['asked_questions'][0]['question']['title']}
    finding = {'ruleId': 'question_wording', 'field': 'title', 'reason': '题干范围不明确，请限定为一顿正餐。'}
    model = SurveyRewriteModel([corrected], finding=finding, proposal=duplicate)
    await plan_with_lifestyle_question(model, original)

    result = await HealthAnalysisGenerator(model).generate(data)

    revised = next(q for q in questions(result) if q['name'] == 'ha_diet_structure')
    assert revised['title'] == corrected['title']
    (rewrite,) = [body for stage, body in model.calls if stage == 'survey_rewrite']
    reviewed_issue, program_issue = rewrite['feedback'][0]['issues']
    assert reviewed_issue == finding
    assert program_issue['ruleId'] == 'revision_validation'
    assert program_issue['field'] == 'question'
    assert 'exact_history_duplicate' in program_issue['reason']
    (checked,) = [body for stage, body in model.calls if stage == 'review'][-1]['questions']
    assert checked['question']['title'] == corrected['title']
    assert {'semanticReview', 'ageApplicability'}.isdisjoint(checked)


@pytest.mark.asyncio
async def test_invalid_rewrite_uses_remaining_budget_with_validation_feedback():
    original = template_question('diet_structure')
    corrected = {'title': '一顿正餐里，您通常吃哪些类食物？'}
    finding = {'ruleId': 'question_wording', 'field': 'title', 'reason': '请明确采集范围。'}
    model = SurveyRewriteModel([{'choices': []}, corrected], finding=finding)
    await plan_with_lifestyle_question(model, original)

    result = await HealthAnalysisGenerator(model, max_survey_rewrites=2).generate(snapshot(materials=[material()]))

    first, second = [body for stage, body in model.calls if stage == 'survey_rewrite']
    assert first['questions'] == second['questions']
    (feedback,) = second['feedback']
    assert feedback['issues'][0] == finding
    assert feedback['issues'][1]['ruleId'] == 'revision_validation'
    assert '未通过题型、选项或适用性规则' in feedback['issues'][1]['reason']
    final_review = [body for stage, body in model.calls if stage == 'review'][-1]
    assert final_review['questions'][0]['question']['title'] == corrected['title']
    revised = next(row for row in questions(result) if row['name'] == 'ha_diet_structure')
    assert revised['title'] == corrected['title']


@pytest.mark.asyncio
async def test_invalid_rewrites_fail_when_all_attempts_are_used():
    original = template_question('diet_structure')
    finding = {'ruleId': 'question_wording', 'field': 'title', 'reason': '请明确采集范围。'}
    model = SurveyRewriteModel([{'choices': []}, {'choices': []}], finding=finding)
    await plan_with_lifestyle_question(model, original)

    with pytest.raises(HealthAnalysisGenerationError) as failure:
        await HealthAnalysisGenerator(model, max_survey_rewrites=2).generate(snapshot(materials=[material()]))

    assert failure.value.stage is AnalysisGenerationStage.SURVEY_REWRITE
    assert failure.value.reason is AnalysisGenerationFailure.SURVEY_REWRITE_INVALID
    assert [stage for stage, _ in model.calls].count('survey_rewrite') == 2
    assert [stage for stage, _ in model.calls].count('review') == 1


@pytest.mark.asyncio
async def test_unresolved_content_failure_stops_at_the_correction_limit():
    original = template_question('diet_structure')
    replacements = [
        {**original, 'title': '一顿正餐里，您通常吃哪些食物？'},
        {**original, 'title': '您的一顿正餐通常搭配哪些食物？'},
    ]
    finding = {'ruleId': 'question_wording', 'field': 'title', 'reason': '请明确采集范围。'}
    model = SurveyRewriteModel(replacements, finding=finding, repeat=True)
    await plan_with_lifestyle_question(model, original)

    with pytest.raises(HealthAnalysisGenerationError, match='within the rewrite budget') as failure:
        await HealthAnalysisGenerator(model, max_survey_rewrites=2).generate(snapshot(materials=[material()]))

    assert failure.value.stage is AnalysisGenerationStage.REVIEW
    assert failure.value.reason is AnalysisGenerationFailure.SURVEY_REVIEW_EXHAUSTED
    assert sum(stage == 'survey_rewrite' for stage, _ in model.calls) == 2
    assert sum(stage == 'review' for stage, _ in model.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('key', 'reason'),
    [
        ('questions', AnalysisGenerationFailure.REVIEW_QUESTIONS_MISSING),
        ('surveyFindings', AnalysisGenerationFailure.REVIEW_RULES_MISSING),
        ('verdict', AnalysisGenerationFailure.REVIEW_VERDICTS_INVALID),
        ('novel', AnalysisGenerationFailure.REVIEW_VERDICTS_INVALID),
        ('draftVersion', AnalysisGenerationFailure.REVIEW_VERSION_MISMATCH),
        ('ruleId', AnalysisGenerationFailure.REVIEW_RULES_INVALID),
        ('interviews', AnalysisGenerationFailure.REVIEW_RECOMMENDATIONS_INVALID),
    ],
)
async def test_persistently_malformed_joint_review_retries_once_and_cannot_publish(key, reason):
    class MalformedSurveyReviewer(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review' and payload.get('survey'):
                output = response['output']
                if key == 'verdict':
                    output['questions'][-1]['verdict'] = 'apprvoed'
                elif key == 'novel':
                    output['questions'][-1].pop('novel')
                elif key == 'draftVersion':
                    output['draftVersion'] += 1
                elif key == 'ruleId':
                    output['surveyFindings'][-1]['ruleId'] = 'unknown_rule'
                elif key == 'interviews':
                    output['interviews'] = {}
                else:
                    output[key].pop()
            return response

    model = MalformedSurveyReviewer()
    with pytest.raises(HealthAnalysisGenerationError) as failure:
        await HealthAnalysisGenerator(model).generate(snapshot(history=True))

    assert failure.value.stage is AnalysisGenerationStage.REVIEW
    assert failure.value.reason is reason
    assert [stage for stage, _ in model.calls] == ['coverage', 'review', 'review']
    first, retry = [body for stage, body in model.calls if stage == 'review']
    assert retry == {**first, 'survey': {**first['survey'], 'protocolFeedback': reason.value}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('key', 'reason'),
    [
        ('questions', AnalysisGenerationFailure.REVIEW_QUESTIONS_MISSING),
        ('surveyFindings', AnalysisGenerationFailure.REVIEW_RULES_MISSING),
    ],
)
async def test_joint_review_recovers_missing_decisions_on_the_same_draft(key, reason):
    class IncompleteFirstReview(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review' and 'protocolFeedback' not in payload['survey']:
                response['output'][key].pop()
            return response

    model = IncompleteFirstReview()
    output = await HealthAnalysisGenerator(model).generate(snapshot(history=True))

    assert [stage for stage, _ in model.calls] == ['coverage', 'review', 'review']
    first, retry = [body for stage, body in model.calls if stage == 'review']
    assert retry == {**first, 'survey': {**first['survey'], 'protocolFeedback': reason.value}}
    assert questions(output) == [row['question'] for row in first['questions']]
    assert output['trace']['surveyProtocolRecoveries'] == [
        {'draftVersion': first['survey']['draftVersion'], 'reason': reason.value}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('candidate_id', 'field', 'value'),
    [('q0_diet_structure', 'novel', None), ('medical_history', 'suitable', 'true'), ('q0_diet_structure', 'novel', 1)],
)
async def test_joint_review_retries_malformed_approval_flags_on_the_same_draft(candidate_id, field, value):
    class MalformedFirstApproval(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review' and 'protocolFeedback' not in payload['survey']:
                decision = next(row for row in response['output']['questions'] if row['candidateId'] == candidate_id)
                if value is None:
                    decision.pop(field)
                else:
                    decision[field] = value
            return response

    model = MalformedFirstApproval()
    output = await HealthAnalysisGenerator(model).generate(snapshot(history=True))

    assert [stage for stage, _ in model.calls] == ['coverage', 'review', 'review']
    first, retry = [body for stage, body in model.calls if stage == 'review']
    reason = AnalysisGenerationFailure.REVIEW_VERDICTS_INVALID.value
    assert retry == {**first, 'survey': {**first['survey'], 'protocolFeedback': reason}}
    assert questions(output) == [row['question'] for row in first['questions']]
    assert output['trace']['surveyProtocolRecoveries'] == [
        {'draftVersion': first['survey']['draftVersion'], 'reason': reason}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('rule_id', 'field'),
    [('unregistered_rule', 'title'), ('question_wording', 'choices'), ('bedtime_clock', 'choices')],
)
async def test_joint_review_rejects_invalid_question_issue_without_a_correction_budget(rule_id, field):
    finding = {'ruleId': rule_id, 'field': field, 'reason': '请修正当前题目。'}
    model = SurveyRewriteModel(finding=finding)
    await plan_with_lifestyle_question(model, template_question('diet_structure'))

    with pytest.raises(HealthAnalysisGenerationError, match='invalid question issue') as failure:
        await HealthAnalysisGenerator(model, max_survey_rewrites=0).generate(snapshot(materials=[material()]))

    assert failure.value.stage is AnalysisGenerationStage.REVIEW
    assert failure.value.reason is AnalysisGenerationFailure.REVIEW_ISSUES_INVALID
    assert [stage for stage, _ in model.calls].count('review') == 1
    assert not any(stage == 'survey_rewrite' for stage, _ in model.calls)


@pytest.mark.asyncio
async def test_protocol_retry_consumes_one_questionnaire_correction():
    class IncompleteFirstReview(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review':
                verdicts = response['output']['questions']
                if 'protocolFeedback' not in payload['survey']:
                    verdicts.pop()
                else:
                    verdicts[-1].update(
                        verdict='requires_revision',
                        issues=[{'ruleId': 'question_wording', 'field': 'title', 'reason': '请明确题目范围。'}],
                    )
            return response

    model = IncompleteFirstReview()
    with pytest.raises(HealthAnalysisGenerationError, match='within the rewrite budget') as failure:
        await HealthAnalysisGenerator(model, max_survey_rewrites=1).generate(snapshot(history=True))

    assert failure.value.reason is AnalysisGenerationFailure.SURVEY_REVIEW_EXHAUSTED
    assert [stage for stage, _ in model.calls] == ['coverage', 'review', 'review']


@pytest.mark.asyncio
async def test_revising_background_options_preserves_explicit_unknown_and_denial():
    class BackgroundReviewer(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review' and payload['survey']['draftVersion'] == 1:
                question = next(row['question'] for row in payload['questions'] if row['targetId'] == 'medical_history')
                decision = next(
                    row for row in response['output']['questions'] if row['candidateId'] == 'medical_history'
                )
                choices = [option for option in question['choices'] if option['text'] != '不清楚']
                decision.update(
                    verdict='requires_revision',
                    issues=[{'ruleId': 'options_match_question', 'field': 'choices', 'reason': '补充一种疾病选项。'}],
                    replacement={
                        'choices': [*choices, {'value': 'asthma', 'text': '哮喘'}],
                        'showNoneItem': False,
                        'noneValue': None,
                        'noneText': None,
                    },
                )
            return response

    output = await HealthAnalysisGenerator(BackgroundReviewer()).generate(snapshot(history=True))

    question = next(q for q in questions(output) if q['name'] == 'medical_history')
    assert question['choices'][-2:] == [{'value': 'asthma', 'text': '哮喘'}, {'value': '8', 'text': '不清楚'}]
    assert question['showNoneItem'] is True
    assert question['noneValue'] == 'none'
    assert question['noneText'] == '以上均无'
    _, denial = history_answer(question, ['none'])
    assert denial['status'] == 'denied'
    assert denial['content']['readableAnswer'] == '以上均无'


@pytest.mark.asyncio
async def test_survey_level_issue_triggers_repair_even_if_each_question_was_approved():
    class CompositionReviewer(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review' and payload['survey']['draftVersion'] == 1:
                output = response['output']
                finding = next(row for row in output['surveyFindings'] if row['ruleId'] == 'survey_composition')
                finding.update(
                    status='requires_revision',
                    questionIds=['q0_diet_structure'],
                    reason='题目的时间范围与整卷不一致',
                )
                decision = next(row for row in output['questions'] if row['candidateId'] == 'q0_diet_structure')
                decision['replacement'] = {'title': '您最近一周的正餐通常包含哪些食物？'}
            return response

    model = CompositionReviewer()
    output = await HealthAnalysisGenerator(model).generate(snapshot(history=True))
    reviews = [body for stage, body in model.calls if stage == 'review']
    assert len(reviews) == 2 and len(reviews[1]['questions']) == 1
    assert reviews[1]['survey']['previousFindings'][0]['ruleId'] == 'survey_composition'
    assert any(q['title'] == '您最近一周的正餐通常包含哪些食物？' for q in questions(output))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('statement', 'cited', 'applicable_exclusion'),
    [('我从不喝酒。', True, True), ('我从不喝酒。', False, False), ('我每天骑车。', True, False)],
)
async def test_delete_inapplicable_question_requires_grounded_evidence(statement, cited, applicable_exclusion):

    class ApplicabilityReviewer(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review':
                for decision in response['output']['questions']:
                    if decision['candidateId'] == 'q0_alcohol_amount':
                        decision.update(
                            verdict='inapplicable',
                            suitable=False,
                            reason='本人已明确表示不饮酒',
                            materialEvidence=[{'evidenceId': 'm1', 'quote': statement}] if cited else [],
                        )
            return response

    model = ApplicabilityReviewer(states={key: 'material_covered' for key in DEFAULT_ORDER if key != 'alcohol_amount'})
    generator = HealthAnalysisGenerator(model, max_survey_rewrites=0)
    data = snapshot(materials=[material('chat', statement)])
    if not applicable_exclusion:
        with pytest.raises(HealthAnalysisGenerationError, match='within the rewrite budget'):
            await generator.generate(data)
        return
    result = await generator.generate(data)

    assert questions(result) == background_questions()
    covered = next(row for row in result['trace']['coverage'] if row['targetId'] == 'alcohol_amount')
    assert covered['status'] == 'inapplicable' and covered['materialRefs'] == ['m1']
    assert [body['survey']['questionIds'] for stage, body in model.calls if stage == 'review'] == [
        ['health_goals', 'medical_history', 'family_history', 'q0_alcohol_amount']
    ]
    assert not any(stage == 'survey_rewrite' for stage, _ in model.calls)


@pytest.mark.asyncio
async def test_incomplete_coverage_cannot_be_returned_as_an_empty_success():
    class MissingCoverage(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'coverage':
                response['output']['coverage'] = []
            return response

    with pytest.raises(HealthAnalysisGenerationError):
        await HealthAnalysisGenerator(MissingCoverage()).generate(snapshot(history=True))


@pytest.mark.asyncio
async def test_open_interview_about_same_topic_retains_more_detail_and_bad_question_uses_safe_template():
    model = StageModel()
    base = (await model.run('plan', {'materials': [material()]}))['output']
    base['advancedQuestions'] = [
        {
            'question': '请描述日常活动的具体安排和实际困难。',
            'purpose': '了解执行条件',
            'collectionGoal': '具体场景和困难',
            'materialIds': ['m1'],
        }
    ]
    bad = template_question('activity')
    bad['choices'][0]['text'] = '其他请填写'
    base['questions'] = [{'targetId': 'activity', 'question': bad}]
    model.plan = base
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))
    assert '具体安排和实际困难' in output['result']['advancedQuestions']
    assert all('其他请填写' not in str(q) for q in questions(output))
    assert any(f['reason'] == 'invalid_question' for f in output['trace']['filters'])


@pytest.mark.asyncio
@pytest.mark.parametrize('gender,body', [('male', '子宫情况'), ('female', '前列腺情况'), (None, '性功能情况')])
async def test_gender_gate_checks_open_body_and_question_options_only_from_profile(gender, body):
    model = StageModel()
    plan = (await model.run('plan', {'materials': [material()]}))['output']
    plan['advancedQuestions'] = [
        {'question': f'请介绍{body}。', 'purpose': '合成目的', 'collectionGoal': '具体经过', 'materialIds': ['m1']}
    ]
    question = template_question('activity')
    question['choices'][0]['text'] = body
    plan['questions'] = [{'targetId': 'activity', 'question': question}]
    model.plan = plan
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()], gender=gender))
    assert body not in output['result']['advancedQuestions']
    assert all(body not in str(q) for q in questions(output))


@pytest.mark.asyncio
async def test_program_errors_and_cancellation_are_not_converted_to_basic_questionnaires():
    class Broken:
        def __init__(self, error):
            self.error = error

        async def run(self, stage, payload):
            raise self.error

    for error in (ValueError('synthetic program error'), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            await HealthAnalysisGenerator(Broken(error)).generate(snapshot(materials=[material()]))


@pytest.mark.asyncio
async def test_input_snapshot_is_not_modified_by_generation_and_model_identities_are_retained():
    data = snapshot(materials=[material()], history=True)
    original = deepcopy(data)
    output = await HealthAnalysisGenerator(StageModel()).generate(data)
    assert data == original
    assert all(row['provider'] == 'offline' for row in output['model_identity']['calls'])
    assert set(output['result']) == {'healthReview', 'suggestedMaterials', 'advancedQuestions', 'advancedSurveySchema'}
    assert output['trace']['contentBindings']['healthReview']['materialIds'] == ['m1']


@pytest.mark.asyncio
async def test_deadline_is_enforced_and_never_sent_as_personal_model_input():
    class TimedModel(StageModel):
        async def run(self, stage, payload, *, deadline_at=None):
            assert deadline_at is not None
            assert 'deadline_at' not in str(payload)
            return await super().run(stage, payload)

    data = snapshot(materials=[material()])
    data['deadline_at'] = asyncio.get_running_loop().time() + 30
    model = TimedModel()
    await HealthAnalysisGenerator(model).generate(data)
    previous = len(model.calls)
    data['deadline_at'] = asyncio.get_running_loop().time() - 1
    with pytest.raises(AnalysisTimeoutError):
        await HealthAnalysisGenerator(model).generate(data)
    assert len(model.calls) == previous


@pytest.mark.asyncio
async def test_malformed_chat_classification_or_missing_identity_cannot_be_a_missing_data_success():
    class BadChat(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'materials':
                response['output']['items'][0]['id'] = []
            return response

    with pytest.raises(HealthAnalysisGenerationError):
        await HealthAnalysisGenerator(BadChat()).generate(snapshot(materials=[material('chat', '合成个人情况')]))


@pytest.mark.asyncio
async def test_unhashable_model_coverage_values_are_contract_errors_not_programming_errors():
    class BadCoverage(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'coverage':
                for row in response['output']['coverage']:
                    row['status'] = {}
            return response

    with pytest.raises(HealthAnalysisGenerationError):
        await HealthAnalysisGenerator(BadCoverage()).generate(snapshot(history=True))


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['exact', 'punctuation'])
async def test_program_guard_rejects_old_question_even_when_both_model_stages_falsely_approve(variant):
    model = StageModel()
    data = snapshot(history=True)
    if variant == 'punctuation':
        title = data['asked_questions'][0]['question']['title']
        data['asked_questions'][0]['question']['title'] = '  ' + ' '.join(title[:-1]) + '！！ '
    old = normalized_title(data['asked_questions'][0]['question']['title'])
    output = await HealthAnalysisGenerator(model).generate(data)
    assert all(normalized_title(q['title']) != old for q in questions(output))
    assert len(questions(output)) == 20
    assert any(row['reason'] == 'exact_history_duplicate' for row in output['trace']['filters'])
    assert (
        next(row for row in output['trace']['coverage'] if row['targetId'] == 'exercise_frequency')['status']
        == 'askable'
    )


@pytest.mark.asyncio
async def test_two_targets_cannot_publish_the_same_title_despite_false_novelty_approval():
    model = StageModel()
    plan = (await model.run('plan', {'materials': [material()]}))['output']
    shared = template_question('diet_structure')
    second = deepcopy(shared)
    second['title'] = shared['title'].replace('？', '！')
    plan['questions'] = [
        {'targetId': 'diet_structure', 'question': shared},
        {'targetId': 'staple_portion', 'question': second},
    ]
    model.plan = plan
    output = await HealthAnalysisGenerator(model).generate(snapshot(materials=[material()]))
    titles = [normalized_title(q['title']) for q in questions(output)]
    assert len(titles) == len(set(titles))
    assert any(row['reason'] == 'exact_new_duplicate' for row in output['trace']['filters'])


@pytest.mark.asyncio
async def test_template_correction_cannot_duplicate_a_question_approved_in_the_first_round():
    askable = {*DEFAULT_ORDER[:6], 'exercise_frequency'}
    states = {key: 'history_covered' for key in DEFAULT_ORDER if key not in askable}
    model = StageModel(states=states)
    plan = (await model.run('plan', {'materials': [material()]}))['output']
    plan['questions'] = [{'targetId': 'sleep', 'question': template_question('exercise_frequency')}]
    model.plan = plan
    data = snapshot(materials=[material()], history=True)
    data['asked_questions'][0]['question']['title'] = '合成已问历史内容'
    with pytest.raises(HealthAnalysisGenerationError, match='complete catalogue coverage'):
        await HealthAnalysisGenerator(model).generate(data)
    reviews = [payload for stage, payload in model.calls if stage == 'review']
    assert all(not (q['targetId'] == 'exercise_frequency') for payload in reviews for q in payload['questions'])


def test_exact_title_normalization_does_not_merge_distinct_numeric_conditions():
    assert normalized_title('每天超过1.5升吗？') != normalized_title('每天超过15升吗？')
    assert normalized_title('每周运动１～２天？') == normalized_title(' 每周运动1～2天！ ')


@pytest.mark.asyncio
async def test_same_choices_on_different_lifestyle_questions_do_not_prove_duplication():
    output = await HealthAnalysisGenerator(StageModel()).generate(snapshot(history=True))
    names = {q['name'] for q in questions(output)}
    assert {'ha_sweet_intake', 'ha_salt_processed_food'}.issubset(names)


@pytest.mark.asyncio
async def test_scope_query_exclusions_are_popped_before_every_model_stage_and_never_retained_in_target_trace():
    private_ids = ['synthetic-scope-user-8721', 'synthetic-scope-group-9921', 'synthetic-sender-7721']

    class GuardedModel(StageModel):
        async def run(self, stage, payload):
            assert 'query_exclusion_values' not in str(payload)
            assert all(value not in str(payload) for value in private_ids)
            response = await super().run(stage, payload)
            if stage == 'targets':
                response['output']['targets'] = [
                    {
                        'label': '合成关注方向',
                        'kind': 'health_goal',
                        'query': private_ids[0] + ' 合成医学概念',
                        'interpretation': '仅按实际资料说明合成目标',
                        'personalEvidence': [{'evidenceId': 'm1', 'quote': '合成独立陈述完整'}],
                    }
                ]
            return response

    data = snapshot(materials=[material(content='合成独立陈述完整')])
    data['query_exclusion_values'] = private_ids
    model = GuardedModel()
    output = await HealthAnalysisGenerator(model).generate(data)
    assert output['trace']['factors']['filters'] == [{'stage': 'targets', 'reason': 'invalid_target'}]
    assert all(value not in str(output) for value in private_ids)
    assert {stage for stage, _ in model.calls} == {'targets', 'plan', 'coverage', 'review'}


@pytest.mark.asyncio
async def test_reworded_semantic_duplicate_in_a_correction_round_is_compared_with_retained_questions():
    askable = {*DEFAULT_ORDER[:6], 'exercise_frequency'}
    states = {key: 'history_covered' for key in DEFAULT_ORDER if key not in askable}

    class CrossRoundReviewer(StageModel):
        async def run(self, stage, payload):
            response = await super().run(stage, payload)
            if stage == 'review':
                retained = [row for row in payload['retainedQuestions'] if row['question']['name'].startswith('ha_')]
                for row in response['output']['questions']:
                    candidate = next(q for q in payload['questions'] if q['candidateId'] == row['candidateId'])
                    if candidate['targetId'] == 'exercise_frequency':
                        row['verdict'] = 'duplicate' if retained else 'uncertain'
                        if retained:
                            assert any('锻炼的天数' in q['question']['title'] for q in retained)
            return response

    model = CrossRoundReviewer(states=states)
    plan = (await model.run('plan', {'materials': [material()]}))['output']
    custom = template_question('exercise_frequency')
    custom['title'] = '您一周安排锻炼的天数通常是多少？'
    plan['questions'] = [{'targetId': 'sleep', 'question': custom}]
    model.plan = plan
    data = snapshot(materials=[material()], history=True)
    data['asked_questions'][0]['question']['title'] = '合成已问历史内容'
    with pytest.raises(HealthAnalysisGenerationError, match='complete catalogue coverage'):
        await HealthAnalysisGenerator(model).generate(data)
    reviews = [payload for stage, payload in model.calls if stage == 'review']
    assert len(reviews) == 2 and len(reviews[1]['retainedQuestions']) == 9
    assert any(q['targetId'] == 'exercise_frequency' for q in reviews[1]['questions'])
    coverages = [body for stage, body in model.calls if stage == 'coverage']
    assert len(coverages) == 2 and 'backgroundTargets' in coverages[0] and not coverages[1].get('backgroundTargets')


@pytest.mark.asyncio
async def test_all_model_stages_share_frozen_effective_age_and_gender_without_name():
    model = StageModel()
    value = snapshot(materials=[material()], gender='female')
    value['query_exclusion_values'] = ['synthetic-private-name']
    await HealthAnalysisGenerator(model).generate(value)
    assert model.calls
    for _, model_payload in model.calls:
        assert model_payload['effectiveDemographics'] == value['effective_demographics']
        assert 'query_exclusion_values' not in model_payload
        assert 'synthetic-private-name' not in str(model_payload)


@pytest.mark.asyncio
async def test_confirmed_name_in_chat_and_answers_is_projected_before_every_model_call():
    model = StageModel()
    value = snapshot(
        materials=[
            material('chat', '我是姓名哨兵，最近活动少'),
            {'id': 'm2', 'kind': 'answer', 'content': '姓名哨兵每晚睡眠不足'},
        ],
        gender='female',
    )
    value['model_redaction_names'] = ['姓名哨兵']
    original = deepcopy(value)
    result = await HealthAnalysisGenerator(model).generate(value)
    assert model.calls and all('姓名哨兵' not in str(body) for _, body in model.calls)
    assert result['trace']['modelInputProjection']['changes']
    assert '姓名哨兵' in str(result['trace']['modelInputProjection'])
    assert value == original


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['age', 'male', 'background'])
async def test_legal_short_names_do_not_corrupt_protocol_keys_or_gender_enums(name):
    model = StageModel()
    value = snapshot(materials=[material('answer', name.upper() + ' provided synthetic text')], gender='male')
    value['model_redaction_names'] = [name]
    result = await HealthAnalysisGenerator(model).generate(value)
    for _, body in model.calls:
        assert body['effectiveDemographics']['ageYears'] == 42
        assert body['effectiveDemographics']['gender'] == 'male'
    assert result['trace']['modelInputProjection']['changes']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'name,variants',
    [
        ('Synthetic Alice', ('SYNTHETIC ALICE', 'Ｓｙｎｔｈｅｔｉｃ　ＡＬＩＣＥ', 'sYnThEtIc aLiCe')),
        ('Straße Café', ('STRASSE CAFE\u0301', 'ＳＴＲＡＳＳＥ ＣＡＦÉ', 'strasse café')),
    ],
)
async def test_normalized_name_is_redacted_before_all_model_stages_with_exact_trace(name, variants):
    model = StageModel()
    value = snapshot(
        materials=[
            material('answer', f'Before {variants[0]} after'),
            {'id': 'm2', 'kind': 'report', 'content': f'Before {variants[1]} after'},
            {'id': 'm3', 'kind': 'chat', 'content': f'Before {variants[2]} after'},
        ],
        gender='female',
    )
    value['model_redaction_names'] = [name]
    original = deepcopy(value)

    result = await HealthAnalysisGenerator(model).generate(value)

    assert {'materials', 'targets', 'plan', 'coverage', 'review'} <= {stage for stage, _ in model.calls}
    assert all(variant not in str(body) for variant in variants for _, body in model.calls)
    changes = result['trace']['modelInputProjection']['changes']
    assert [change['originalValue'] for change in changes] == [f'Before {variant} after' for variant in variants]
    assert all(change['projectedValue'].startswith('Before [') for change in changes)
    assert all(change['projectedValue'].endswith('] after') for change in changes)
    assert value == original


@pytest.mark.asyncio
async def test_report_field_names_do_not_exempt_nested_text_from_model_redaction():
    model = StageModel()
    variant = 'ＳＹＮＴＨＥＴＩＣ　ＡＬＩＣＥ'
    value = snapshot(
        materials=[
            {
                'id': 'report:synthetic',
                'kind': 'report',
                'status': 'answered',
                'content': {
                    'gender': variant,
                    'type': {'kind': variant},
                    'status': [{'id': variant, 'classification': variant}],
                    'question': {'type': variant, 'questionName': variant},
                },
                'metadata': {
                    'kind': variant,
                    'status': {'sourceReliability': variant},
                    'effectiveDemographics': {'gender': variant},
                },
            },
        ],
        gender='female',
    )
    value['model_redaction_names'] = ['Synthetic Alice']
    original = deepcopy(value)

    result = await HealthAnalysisGenerator(model).generate(value)

    assert {'targets', 'plan', 'coverage', 'review'} <= {stage for stage, _ in model.calls}
    assert all(variant not in str(body) for _, body in model.calls)
    report = next(body['materials'][0] for stage, body in model.calls if stage == 'targets')
    assert report['kind'] == 'report' and report['status'] == 'answered'
    trace = result['trace']['modelInputProjection']['changes']
    assert {change['pointer'] for change in trace} == {
        '/personal_materials/0/content/gender',
        '/personal_materials/0/content/type/kind',
        '/personal_materials/0/content/status/0/id',
        '/personal_materials/0/content/status/0/classification',
        '/personal_materials/0/content/question/type',
        '/personal_materials/0/content/question/questionName',
        '/personal_materials/0/metadata/kind',
        '/personal_materials/0/metadata/status/sourceReliability',
        '/personal_materials/0/metadata/effectiveDemographics/gender',
    }
    assert all(change['originalValue'] == variant for change in trace)
    assert value == original


@pytest.mark.asyncio
async def test_report_text_is_redacted_even_when_equal_to_controlled_values():
    model = StageModel()
    value = snapshot(
        materials=[
            {
                'id': 'report:synthetic',
                'kind': 'report',
                'status': 'answered',
                'content': {'gender': 'male', 'question': {'type': 'male'}},
                'metadata': {'gender': 'male'},
            },
        ],
        gender='male',
    )
    value['model_redaction_names'] = ['male']
    result = await HealthAnalysisGenerator(model).generate(value)

    for _, body in model.calls:
        assert body['effectiveDemographics']['gender'] == 'male'
        for row in body.get('materials', []):
            assert 'male' not in str(row['content'])
            assert 'male' not in str(row['metadata'])
    assert len(result['trace']['modelInputProjection']['changes']) == 3
