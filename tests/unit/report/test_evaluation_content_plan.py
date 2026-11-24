"""Content planning fails per item and per section, never as a whole plan."""

import asyncio
from types import SimpleNamespace

from modules.report.clinical_source_projection import get_patient_projection
from modules.report.evaluation_content_plan import plan_assessment_content

CHECKS = {
    key: True
    for key in (
        'sourceBindingsValid',
        'sourceNaturePreserved',
        'personalHistoryNotInvented',
        'relatedObservationsSelected',
        'problemsIntegratedWithoutDuplication',
        'clinicalPrioritySupported',
        'noUnsupportedCausation',
        'themesAreSharedManagementQuestions',
        'normalAndRoutineFindingsNotSeparateClusters',
    )
}


def clinical(fid, name, value, *, unit=None, reference=None, status=None, abnormal=False, imaging=False, exam=False):
    row = {
        'factId': fid,
        'kind': 'clinical',
        'name': name,
        'value': value,
        'unit': unit,
        'referenceRange': reference,
        'status': status,
        'abnormal': abnormal,
        'imaging': imaging,
        'source': None,
    }
    source = SimpleNamespace(**row)
    source._clinical_source_context = {'dataType': 'EXAM' if exam or imaging else 'INSPECTION'}
    return row, get_patient_projection(source)


def reported(fid, name, value):
    return {'factId': fid, 'kind': 'self_reported', 'name': name, 'value': value}, None


def blueprint(rows, *, focus=(), roles=None, slots=(), actions=()):
    return SimpleNamespace(
        facts=[row for row, _ in rows],
        chapters=[],
        focus=list(focus),
        assessment_sources={
            'clinicalPatientProjections': {row['factId']: projection for row, projection in rows if projection},
            'reportedSourceRoles': roles or {},
            'symptomSlots': list(slots),
            'clinicalActions': list(actions),
        },
        clinical_trends=[],
    )


def person_with_findings():
    """Synthetic 58-year-old woman: positive FOBT, raised BP, a gallbladder polyp."""
    rows = [
        clinical('fobt', '粪便隐血试验', '弱阳性', reference='阴性', status='阳性', abnormal=True),  # F0001
        clinical('hb', '血红蛋白', '145', unit='g/L', reference='115-150'),  # F0002
        clinical('sbp', '血压(收缩压)', '152', unit='mmHg', reference='90-139', status='偏高', abnormal=True),  # F0003
        clinical('exam', '内科问诊记录', '既往史：高血压3年；吸烟：从不；饮酒：从不。心律齐', exam=True),  # F0004
        clinical(
            'us', '腹部彩超', '胆囊壁见中等回声附着，长径约0.4cm，不随体位移动；胆囊息肉', imaging=True, abnormal=True
        ),  # F0005
        reported('tired', '容易疲劳', '容易疲劳'),  # F0006
        reported('sport', '您平时一周大约有几天安排运动？', '没有运动'),  # F0007
        reported('salt', '您平时吃得咸吗？', '偏咸'),  # F0008
    ]
    focus = [
        {
            'code': 'pF',
            'name': '粪便隐血弱阳性',
            'role': 'health_problem',
            'focusLabel': '重点管理',
            'category': 'intestinal',
            'factIds': ['fobt'],
        },
        {
            'code': 'pB',
            'name': '血压升高',
            'role': 'health_problem',
            'focusLabel': '重点管理',
            'category': 'cardio',
            'factIds': ['sbp', 'exam'],
        },
        {
            'code': 'pG',
            'name': '胆囊息肉',
            'role': 'health_problem',
            'focusLabel': '持续管理',
            'category': 'biliary',
            'factIds': ['us'],
        },
    ]
    return blueprint(
        rows,
        focus=focus,
        roles={'tired': {'role': 'reported_symptom'}},
        slots=[{'code': 'S1', 'name': '容易疲劳', 'factIds': ['tired']}],
        actions=[{'code': 'pG', 'originalAdvice': ['每6个月复查胆囊B超一次，肝胆外科随诊。'], 'sourceFactIds': ['us']}],
    )


def cluster(code, priority, clinical_ids, problems, *, fact_ids=(), selections=()):
    return {
        'code': code,
        'name': code + ' 主题',
        'priority': priority,
        'factIds': list(fact_ids),
        'clinicalFactIds': list(clinical_ids),
        'microbiomeFactIds': [],
        'problemCodes': list(problems),
        'clinicalSelections': list(selections),
        'purpose': code + ' 的管理问题',
    }


GOOD_CLUSTERS = [
    cluster('gi', 'P0', ['F0001'], ['pF']),
    cluster('bp', 'P1', ['F0003'], ['pB'], fact_ids=['F0007', 'F0008']),
]
GOOD_SYMPTOMS = [
    {
        'code': 'S1',
        'name': '容易疲劳',
        'factIds': ['F0006'],
        'clinicalFactIds': [],
        'microbiomeFactIds': [],
        'lifestyleFactIds': [],
        'contextFactIds': [],
        'clinicalSelections': [],
        'purpose': '说明容易疲劳的表现',
    }
]
EXERCISE = {
    'code': 'exercise',
    'name': '运动',
    'factIds': ['F0007'],
    'contextFactIds': [],
    'clusterCodes': [],
    'sourceSelections': [],
    'purpose': '说明运动安排',
}
SMOKING_FROM_EXAM = {
    'code': 'smoking',
    'name': '吸烟',
    'factIds': ['F0004'],
    'contextFactIds': [],
    'clusterCodes': [],
    'sourceSelections': [{'recordId': 'F0004', 'quote': '吸烟：从不；'}],
    'purpose': '说明吸烟情况',
}


class FakePlanner:
    """Answers by (task, section); each value is a response or a callable."""

    def __init__(self, **plans):
        self.plans = plans
        self.calls = []

    async def generate(self, payload):
        self.calls.append(payload)
        section, task = payload['section'], payload['task']
        handler = self.plans.get(f'{task}:{section}', self.plans.get(section))
        if callable(handler):
            return handler(payload)
        if isinstance(handler, Exception):
            raise handler
        if task == 'repair_source_bound_assessment_content_items':
            return {'checks': CHECKS, 'plan': {}}
        return {'checks': CHECKS, 'plan': handler}


def default_plans(**overrides):
    plans = {
        'clusters': {'clusters': GOOD_CLUSTERS},
        'symptoms': {'symptoms': GOOD_SYMPTOMS},
        'lifestyleFactors+recognitions': {'lifestyleFactors': [EXERCISE], 'recognitions': []},
    }
    plans.update(overrides)
    return plans


def run(planner, data, subject=None):
    audit = {}
    plan = asyncio.run(plan_assessment_content(planner, data, subject or {'sex': '女', 'age': 58}, audit=audit))
    return plan, audit


def codes(rows):
    return [row['code'] for row in rows]


def test_one_invalid_lifestyle_item_is_dropped_and_other_sections_stay_usable():
    planner = FakePlanner(
        **default_plans(
            **{
                'lifestyleFactors+recognitions': {
                    'lifestyleFactors': [EXERCISE, SMOKING_FROM_EXAM],
                    'recognitions': [],
                },
            }
        )
    )

    plan, audit = run(planner, person_with_findings())

    assert plan['status'] == 'reviewed'
    assert codes(plan['lifestyleFactors']) == ['exercise']
    assert codes(plan['clusters']) == ['gi', 'bp'] and codes(plan['symptoms']) == ['S1']
    status = plan['sectionStatus']['lifestyleFactors']
    assert status['state'] == 'partial' and 'items_dropped' in status['reasons']
    assert status['dropped'][0]['code'] == 'smoking'
    assert 'not a behavior/scenario anchor' in status['dropped'][0]['errors'][0]
    assert plan['sectionStatus']['clusters']['state'] == 'reviewed'
    assert plan['completeness'] == 'degraded'
    assert audit['contentPlan']['status'] == 'reviewed'
    assert audit['contentPlan']['sectionStatus']['lifestyleFactors']['dropped']
    repair = [call for call in planner.calls if call['task'] == 'repair_source_bound_assessment_content_items']
    assert len(repair) == 1
    assert [item['row']['code'] for item in repair[0]['failedRows']] == ['smoking']
    assert repair[0]['failedRows'][0]['errors']


def test_directed_repair_keeps_a_fixed_item():
    fixed = {**SMOKING_FROM_EXAM, 'code': 'diet', 'name': '饮食', 'factIds': ['F0008'], 'sourceSelections': []}

    def repair(payload):
        assert payload['acceptedRows']['lifestyleFactors'][0]['code'] == 'exercise'
        return {'checks': CHECKS, 'plan': {'lifestyleFactors': [fixed], 'recognitions': []}}

    planner = FakePlanner(
        **default_plans(
            **{
                'lifestyleFactors+recognitions': {
                    'lifestyleFactors': [EXERCISE, SMOKING_FROM_EXAM],
                    'recognitions': [],
                },
                'repair_source_bound_assessment_content_items:lifestyleFactors+recognitions': repair,
            }
        )
    )

    plan, _ = run(planner, person_with_findings())

    assert codes(plan['lifestyleFactors']) == ['exercise', 'diet']
    status = plan['sectionStatus']['lifestyleFactors']
    assert status['repaired'] == ['diet'] and 'items_repaired' in status['reasons']


def test_failed_section_call_degrades_only_that_section_with_rule_fallback():
    planner = FakePlanner(
        **default_plans(
            **{
                'plan_source_bound_assessment_content:symptoms': RuntimeError('model unavailable'),
            }
        )
    )

    plan, _ = run(planner, person_with_findings())

    assert plan['status'] == 'reviewed'
    assert codes(plan['clusters']) == ['gi', 'bp']
    assert [row['name'] for row in plan['symptoms']] == ['容易疲劳']
    assert plan['symptoms'][0]['origin'] == 'rule_fallback'
    status = plan['sectionStatus']['symptoms']
    assert status['state'] == 'fallback'
    assert 'model_call_failed:RuntimeError' in status['reasons'] and 'rule_fallback' in status['reasons']


def test_incomplete_lifestyle_review_falls_back_to_questionnaire_answers():
    def incomplete(payload):
        return {'checks': {**CHECKS, 'sourceBindingsValid': False}, 'plan': payload.get('draftPlan') or {}}

    planner = FakePlanner(
        **default_plans(
            **{
                'review_source_bound_assessment_content:lifestyleFactors+recognitions': incomplete,
            }
        )
    )

    plan, _ = run(planner, person_with_findings())

    assert {row['code'] for row in plan['lifestyleFactors']} == {'exercise', 'diet'}
    assert all(row['origin'] == 'rule_fallback' for row in plan['lifestyleFactors'])
    assert plan['sectionStatus']['lifestyleFactors']['state'] == 'fallback'
    assert plan['sectionStatus']['recognitions']['state'] == 'failed'
    assert 'review_checks_incomplete' in plan['sectionStatus']['recognitions']['reasons']
    assert codes(plan['clusters']) == ['gi', 'bp']


def test_p0_is_granted_only_by_danger_signal_rules():
    planner = FakePlanner(
        **default_plans(
            clusters={
                'clusters': [
                    cluster('gi', 'P1', ['F0001'], ['pF']),
                    cluster('bp', 'P0', ['F0003'], ['pB'], fact_ids=['F0007']),
                ]
            }
        )
    )

    plan, _ = run(planner, person_with_findings())

    by_code = {row['code']: row for row in plan['clusters']}
    assert by_code['gi']['priority'] == 'P0' and by_code['gi']['sourcePriority'] == 'P1'
    assert by_code['gi']['priorityItemCodes'] == ['RF-FOBT-POSITIVE']
    assert by_code['bp']['priority'] == 'P1' and by_code['bp']['sourcePriority'] == 'P0'
    item = plan['priorityItems'][0]
    assert item['ruleId'] == 'RF-FOBT-POSITIVE' and item['clusterCodes'] == ['gi']
    assert item['timeframe']['days'] == 30 and item['department'] == '消化内科'


def test_uncovered_priority_item_gets_a_rule_theme():
    planner = FakePlanner(
        **default_plans(
            clusters={
                'clusters': [
                    cluster('bp', 'P1', ['F0003'], ['pB'], fact_ids=['F0007']),
                ]
            }
        )
    )

    plan, _ = run(planner, person_with_findings())

    p0 = [row for row in plan['clusters'] if row['priority'] == 'P0']
    assert len(p0) == 1 and p0[0]['origin'] == 'red_flag_rule'
    assert p0[0]['priorityItemCodes'] == ['RF-FOBT-POSITIVE'] and p0[0]['problemCodes'] == ['pF']
    status = plan['sectionStatus']['clusters']
    assert 'priority_rule_cluster_added' in status['reasons'] and status['state'] == 'partial'


def test_single_clinical_problem_may_form_a_theme_but_a_routine_follow_up_may_not():
    selection = {'recordId': 'F0005', 'quote': '胆囊壁见中等回声附着，'}
    planner = FakePlanner(
        **default_plans(
            clusters={
                'clusters': [
                    cluster('gi', 'P0', ['F0001'], ['pF']),
                    cluster('bp', 'P1', ['F0003'], ['pB']),
                    cluster('gb', 'P2', ['F0005'], ['pG'], selections=[selection]),
                ]
            }
        )
    )

    plan, _ = run(planner, person_with_findings())

    assert codes(plan['clusters']) == ['gi', 'bp']
    dropped = plan['sectionStatus']['clusters']['dropped']
    assert dropped[0]['code'] == 'gb' and 'routine follow-up' in dropped[0]['errors'][0]
    routine = {item['ruleId']: item for item in plan['routineFollowups']}
    assert routine['RT-GB-POLYP']['interval'] == {'text': '每6个月复查', 'days': 180, 'basis': 'original_advice'}


def test_abnormal_findings_without_reviewed_themes_get_a_rule_theme():
    planner = FakePlanner(**default_plans(clusters={'clusters': []}))

    plan, _ = run(planner, person_with_findings())

    assert plan['clusterState'] == 'clusters_present'
    assert any(row['priority'] == 'P0' and row['origin'] == 'red_flag_rule' for row in plan['clusters'])


def test_problem_without_p0_and_without_themes_gets_fallback_theme():
    data = person_with_findings()
    data.facts[0]['value'] = '阴性'
    data.facts[0]['status'] = None
    data.facts[0]['abnormal'] = False
    data.focus = [problem for problem in data.focus if problem['code'] != 'pF']
    planner = FakePlanner(**default_plans(clusters={'clusters': []}))

    plan, _ = run(planner, data)

    assert not plan['priorityItems']
    assert [row['problemCodes'] for row in plan['clusters']] == [['pB']]
    assert plan['clusters'][0]['origin'] == 'rule_fallback'
    assert plan['sectionStatus']['clusters']['state'] == 'fallback'


def test_all_normal_person_has_no_clusters_and_an_explicit_state():
    rows = [
        clinical('hb', '血红蛋白', '145', unit='g/L', reference='130-175'),
        clinical('sbp', '血压(收缩压)', '118', unit='mmHg', reference='90-139'),
        clinical('ob', '粪便隐血试验', '阴性', reference='阴性'),
        reported('sport', '您平时一周大约有几天安排运动？', '每周3~5天'),
    ]
    planner = FakePlanner(
        clusters={'clusters': []},
        symptoms={'symptoms': []},
        **{
            'lifestyleFactors+recognitions': {
                'lifestyleFactors': [{**EXERCISE, 'factIds': ['F0004']}],
                'recognitions': [],
            }
        },
    )

    plan, _ = run(planner, blueprint(rows), {'sex': '男', 'age': 29})

    assert plan['status'] == 'reviewed'
    assert plan['clusters'] == [] and plan['clusterState'] == 'no_imbalance'
    assert plan['sectionStatus']['clusters'] == {
        'state': 'empty',
        'reasons': ['no_imbalance'],
        'dropped': [],
        'repaired': [],
        'rows': 0,
    }
    assert plan['sectionStatus']['symptoms']['reasons'] == ['no_reported_symptoms']
    assert plan['priorityItems'] == [] and plan['testGaps'] == []
    assert plan['completeness'] == 'complete'


def test_plan_carries_rule_outputs_for_chapters_one_five_and_six():
    planner = FakePlanner(**default_plans())

    plan, _ = run(planner, person_with_findings())

    assert [item['ruleId'] for item in plan['priorityItems']] == ['RF-FOBT-POSITIVE']
    gaps = {gap['code'] for gap in plan['testGaps']}
    assert {'pepsinogen', 'uacr'} <= gaps and 'fit' not in gaps
    assert all(gap['sources'] and gap['reason'] for gap in plan['testGaps'])
    assert plan['rulesVersion'] == {'redFlags': 'assessment-red-flags.v1', 'testGaps': 'assessment-test-gaps.v1'}
    assert plan['completeness'] == 'complete'


def with_body_cell(data):
    data.chapters = [
        {
            'targets': [
                {
                    'path': ['_assessment_editorial', 'bodySummary'],
                    'owner': {'name': '体格概述', 'primaryFactIds': ['hb']},
                }
            ]
        }
    ]
    return data


def test_cell_batch_with_missing_cell_keeps_source_baseline():
    planner = FakePlanner(**default_plans(cellScopes={'cellScopes': []}))

    plan, _ = run(planner, with_body_cell(person_with_findings()))

    cell = next(row for row in plan['cellScopes'] if row['code'] == 'bodySummary')
    assert cell['factIds'] == ['hb'] and cell['contextFactIds'] == []
    status = plan['sectionStatus']['cellScopes']
    assert status['state'] == 'partial'
    assert status['dropped'] == [{'code': 'bodySummary', 'errors': ['cell_context_missing']}]
    assert codes(plan['clusters']) == ['gi', 'bp']


def test_cell_review_failure_falls_back_to_baseline_without_touching_other_sections():
    planner = FakePlanner(
        **default_plans(
            **{
                'review_source_bound_assessment_content:cellScopes': RuntimeError('timeout'),
            }
        )
    )

    plan, _ = run(planner, with_body_cell(person_with_findings()))

    assert any(row['code'] == 'bodySummary' for row in plan['cellScopes'])
    status = plan['sectionStatus']['cellScopes']
    assert status['state'] == 'fallback' and 'source_baseline_used' in status['reasons']
    assert plan['status'] == 'reviewed' and codes(plan['symptoms']) == ['S1']
