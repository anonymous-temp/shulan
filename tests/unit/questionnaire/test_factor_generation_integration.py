"""The candidate plan is frozen with the actual published question IDs."""

from copy import deepcopy

import pytest

from modules.questionnaire.generation import QuestionnaireGenerator
from modules.questionnaire.generation import build_questionnaire
from tests.unit.questionnaire.test_generation import Documents
from tests.unit.questionnaire.test_generation import Planner
from tests.unit.questionnaire.test_generation import plan


class Factors:
    def evidence_rule_selection(self):
        from modules.questionnaire.evidence import LEGACY_EVIDENCE_RULE_SELECTION

        return dict(LEGACY_EVIDENCE_RULE_SELECTION)

    def __init__(self, *, fails=False):
        self.fails = fails
        self.calls = []

    async def prepare(self, **kwargs):
        self.calls.append(('prepare', deepcopy(kwargs)))
        if self.fails:
            raise RuntimeError('synthetic provider failure')
        return {'version': 'v1', 'targets': [], 'candidates': [], 'questionHints': [], 'coverage': {}}

    def bind_questions(self, prepared, questionnaire, planning):
        self.calls.append(('bind', deepcopy(questionnaire)))
        return {**prepared, 'bindings': planning['factorBindings']}


@pytest.mark.asyncio
async def test_preparation_precedes_planning_and_preserves_full_findings():
    reports = [
        {
            'fileIndex': 0,
            'status': 'complete',
            'interpretation': {
                'observations': [
                    {'itemName': f'检查{i}', 'resultValue': '报告原文' * 50, 'flag': 'normal'} for i in range(20)
                ],
            },
        }
    ]
    factors, planner = Factors(), Planner()
    generated = await QuestionnaireGenerator(
        documents=Documents(reports),
        planner=planner,
        factor_pipeline=factors,
    ).generate(user_id='u', questionnaire_id='q', file_list=[{'url': 'https://example.invalid/report.pdf'}])
    assert len(planner.calls[0]['evidence']) == 20
    assert planner.calls[0]['evidence'][0]['value'] == '报告原文' * 50
    assert planner.calls[0]['factor_context']['version'] == 'v1'
    assert factors.calls[0][0] == 'prepare'
    assert factors.calls[0][1]['reports'] == reports
    assert factors.calls[1][1] == generated['questionnaire']
    assert generated['factorPreparation']['version'] == 'v1'


def test_rejected_supplement_never_reassigns_its_condition_to_valid_question():
    planning = plan()
    planning['questions'][0]['factorConditions'] = [{'candidateId': 'f_good', 'conditionId': 'c_good'}]
    rejected = {
        **deepcopy(planning['questions'][0]),
        'choices': ['某种疾病'],
        'factorConditions': [{'candidateId': 'f_bad', 'conditionId': 'c_bad'}],
    }
    planning['questions'].insert(0, rejected)
    planning['factorBindings'] = [
        {'questionName': 'report_follow_up_2', 'candidateId': 'f_wrong', 'conditionId': 'c_wrong'},
        {'questionName': 'medical_history', 'candidateId': 'f_history', 'conditionId': 'c_history'},
    ]
    built = build_questionnaire([{'id': 'r1_1', 'name': '检查', 'value': '8', 'unit': '', 'abnormal': True}], planning)
    assert built['factorBindings'] == [
        {'questionName': 'report_follow_up_1', 'candidateId': 'f_good', 'conditionId': 'c_good'},
        {'questionName': 'medical_history', 'candidateId': 'f_history', 'conditionId': 'c_history'},
    ]
    assert all('factorConditions' not in q for p in built['questionnaire']['pages'] for q in p['elements'])


@pytest.mark.asyncio
async def test_prepare_failure_publishes_base_with_explicit_internal_gap():
    factors, planner = Factors(fails=True), Planner()
    generated = await QuestionnaireGenerator(documents=Documents(), planner=planner, factor_pipeline=factors).generate(
        user_id='u',
        questionnaire_id='q',
        file_list=[{'url': 'https://example.invalid/report.pdf'}],
    )
    assert not planner.calls
    assert generated['mode'] == 'basic'
    assert generated['factorPreparation']['status'] == 'unavailable'
    assert generated['processing']['reason'] == 'factor_preparation_unavailable'
    assert len([q for p in generated['questionnaire']['pages'] for q in p['elements']]) == 10
